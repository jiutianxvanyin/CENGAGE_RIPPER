"""Page layout: rebuilt paragraphs, the paper shape, and the cover.

Three defects showed up in the first real read-through of a captured book:

* a sentence ended mid-line and the rest restarted at the left margin, with
  uneven leading (a glossary term was a ``div``, so it cut the ``<p>`` in two);
* a whole page was chopped into fragments with blank bands between them (a
  parked run of page markers kept the page break inside a zero-height block);
* the footer said ``7`` while the contents said ``vii`` (numbers came from the
  markers, not from the page label the book actually prints).

Each fix gets a test here so it cannot silently come back.
"""
import base64
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fitz                                                    # noqa: E402
import pagenum                                                 # noqa: E402
from pdf_builder import (                                      # noqa: E402
    bleed_cover,
    build_book_html,
    cover_aspect,
    cover_image,
    image_size,
    refragment,
)

from bs4 import BeautifulSoup                                  # noqa: E402


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def _section(inner: str) -> BeautifulSoup:
    return _soup('<html><head><title>t</title></head>'
                 f'<body class="webreader"><section>{inner}</section></body></html>')


def _paras(soup) -> list:
    """Only the paragraphs of the page body, not some nested table cell."""
    host = soup.find("section") or soup
    return host.find_all("p", recursive=False)


def _flat(node) -> str:
    """Text with collapsed whitespace and no space before punctuation (the
    browser collapses a newline to a space, so ``scale\\n . For`` reads as
    ``scale . For`` - which is what a reader would see)."""
    text = " ".join(node.get_text(" ", strip=True).split())
    return re.sub(r"\s+([.,;:])", r"\1", text)


# --------------------------------------------------------------------------- #
# Paragraphs the conversion tool split apart
# --------------------------------------------------------------------------- #
FRAGMENTED_TERM = """
  <p>For this reason, the scale of measurement is considered a </p>
  <div class="keytermentry"><button class="keyterm">nominal scale</button></div>
  . For example, referring to the data in Table 1.1, the variable gender
  divides the data into two categories.
"""


def test_a_glossary_term_does_not_cut_the_paragraph():
    soup = _section(FRAGMENTED_TERM)
    stats = refragment(soup)
    paragraphs = _paras(soup)

    assert len(paragraphs) == 1, "the sentence must be one paragraph again"
    text = _flat(paragraphs[0])
    assert "considered a nominal scale. For example" in text
    assert soup.find("div", class_="keytermentry") is None, "a term is not a block"
    assert soup.find("span", class_="keyterm") is not None
    assert stats["inline"] >= 1 and stats["joined"] >= 1


def test_loose_text_after_a_block_is_wrapped_and_joined():
    """Prose that sits directly in a container used to be laid out at the
    container's width - a paragraph starting at the wrong indent."""
    soup = _section("""
      <p>The left margin shows the row labels
      <span class="keyterm">stub</span></p>
      and the column heads.
    """)
    refragment(soup)
    paragraphs = _paras(soup)
    assert len(paragraphs) == 1
    assert "and the column heads" in paragraphs[0].get_text()


def test_a_sentence_cut_mid_clause_is_rejoined_even_with_a_capital():
    """``…measurement for`` / ``The Fitch Rating is ordinal`` is one sentence:
    there is no terminal punctuation, so the capital is not a new paragraph."""
    soup = _section("""
      <p>it is not meaningful to compute an average, because the scale of
         measurement for</p>
      <p>The Fitch Rating is an example of an ordinal scale.</p>
    """)
    refragment(soup)
    paragraphs = _paras(soup)
    assert len(paragraphs) == 1
    assert "measurement for The Fitch Rating" in _flat(paragraphs[0])


def test_a_footnote_star_does_not_cut_the_paragraph():
    """``div.footnote-wrapper`` around a single ``*`` is the same defect as a
    glossary term: it closed the ``<p>`` and left a one-character line and a
    blank band between two halves of a sentence."""
    soup = _section("""
      <p>The nation's sovereign credit rating as appraised by the Fitch Group</p>
      <div class="footnote-wrapper"><span class="footnoteref"
            role="button">*</span></div>
      ; the credit ratings range from AAA to F.
    """)
    refragment(soup)
    paragraphs = _paras(soup)
    assert len(paragraphs) == 1
    assert soup.find("div", class_="footnote-wrapper") is None
    assert "*; the credit ratings" in _flat(paragraphs[0])


def test_a_display_equation_keeps_its_own_line():
    """``div.mathexpr`` is a display equation, not an inline wrapper - it must
    not be swallowed into the surrounding paragraph."""
    soup = _section("""
      <p>The relative frequency is computed as</p>
      <div class="mathexpr"><header><h5>(2.1)</h5></header>
        <span>Relative frequency of a class</span></div>
      <p>where n is the sample size.</p>
    """)
    refragment(soup)
    assert soup.find("div", class_="mathexpr") is not None
    assert len(_paras(soup)) == 2


def test_real_paragraphs_are_left_alone():
    soup = _section("""
      <p>This paragraph is finished.</p>
      <p>A new paragraph starts here.</p>
      <p>And a third one follows.</p>
    """)
    refragment(soup)
    assert len(_paras(soup)) == 3


def test_headings_and_tables_are_barriers_not_text_to_absorb():
    soup = _section("""
      <h2>1.3 Scales of Measurement</h2>
      <table><tr><td>nominal</td></tr></table>
      <p>body text</p>
    """)
    refragment(soup)
    assert soup.find("h2").get_text() == "1.3 Scales of Measurement"
    assert "body text" not in soup.find("h2").get_text()
    assert soup.find("table") is not None


def test_a_pagebreak_marker_is_a_barrier():
    """Text on the far side of a marker belongs to the next printed page."""
    soup = _section("""
      <p>end of page 57</p>
      <span class="ripper-pb" data-pb="58"></span>
      <p>start of page 58</p>
    """)
    refragment(soup)
    first = _paras(soup)[0]
    assert "start of page 58" not in first.get_text()


# --------------------------------------------------------------------------- #
# A parked run of markers in front of real text
# --------------------------------------------------------------------------- #
PARKED_THEN_TEXT = """
<html xmlns:epub="http://www.idpf.org/2007/ops"><head><title>t</title></head>
<body class="webreader"><section>
  <p>the scale of measurement for</p>
  <div class="block-of-pagenumbers">
    <span class="pagenumber rs_skip" data-after="6" epub:type="pagebreak"
          id="page-6"></span>
    <span class="pagenumber rs_skip" data-after="7" epub:type="pagebreak"
          id="page-7"></span>
  </div>
  <p>The Fitch Rating is an example of an ordinal scale.</p>
</section></body></html>
"""


def test_a_page_break_parked_in_a_zero_height_block_is_hoisted():
    """``_has_content_after(box)`` walked the box's own children first, met the
    second marker and answered "nothing follows", so the real break stayed
    inside a block with no text to push - and the next page's opening line was
    printed on the previous sheet, mid-sentence."""
    breaks = []
    html = build_book_html([PARKED_THEN_TEXT], title="t", breaks=breaks,
                           page_ids=["sec-a"])
    soup = _soup(html)

    assert html.count('<div class="ripper-pb"') == 1, "one printed page starts here"
    assert [b["phantom"] for b in breaks] == [True, False]
    assert soup.find(id="page-7").find_parent(class_="block-of-pagenumbers") is None, \
        "the marker that owns the text below must leave the block"
    assert soup.find(id="page-6").find_parent(class_="block-of-pagenumbers") is not None, \
        "the pages the reader never sent stay put as anchors"
    assert html.index('<div class="ripper-pb"') < html.index("The Fitch Rating")


def test_a_parked_run_with_nothing_after_it_stays_parked():
    """Front matter parks 24 markers for 3 pages of text; hoisting them all
    would print a stack of blank sheets."""
    html_in = ('<html><body class="webreader"><section><h1>About</h1>'
               '<div class="block-of-pagenumbers">'
               '<span class="ripper-pb" data-pb="ii" id="page-ii"></span>'
               '<span class="ripper-pb" data-pb="iii" id="page-iii"></span>'
               '</div></section></body></html>')
    breaks = []
    build_book_html([html_in], title="t", breaks=breaks, page_ids=["s"])
    assert all(b["phantom"] for b in breaks)
    assert len(breaks) == 2


# --------------------------------------------------------------------------- #
# The paper follows the cover
# --------------------------------------------------------------------------- #
def _fake_png(width: int, height: int) -> bytes:
    """Enough of a PNG for the IHDR reader (the pixels are never decoded)."""
    return (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR"
            + width.to_bytes(4, "big") + height.to_bytes(4, "big")
            + b"\x08\x02\x00\x00\x00" + b"\x00" * 8)


def _cover_page(data: bytes, mime: str = "image/png") -> str:
    b64 = base64.b64encode(data).decode()
    return ('<html><head><title>t</title></head><body class="webreader">'
            '<section epub:type="cover">'
            f'<img class="coverimage" epub:type="cover" role="doc-cover" '
            f'src="data:{mime};base64,{b64}"></section></body></html>')


def test_cover_aspect_comes_from_the_scan():
    """506x648 is this book's cover; using it is what turns a 1:2 strip into a
    page shaped like the printed book."""
    page = _cover_page(_fake_png(506, 648))
    assert image_size(cover_image(page)) == (506, 648)
    assert round(cover_aspect(page), 3) == 1.281


def test_a_page_without_a_cover_reports_no_aspect():
    plain = ('<html><body class="webreader"><section><p>text</p>'
             '</section></body></html>')
    assert cover_image(plain) is None
    assert cover_aspect(plain) is None


def test_cover_aspect_ignores_a_remote_image():
    page = ('<html><body class="webreader"><section>'
            '<img class="coverimage" src="https://example.org/c.jpg">'
            '</section></body></html>')
    assert cover_image(page) is None


def test_jpeg_size_is_read_from_the_header():
    """A JPEG carries its size in the first SOF segment, not at a fixed offset."""
    sof = (b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
           b"\xff\xc0\x00\x11\x08"
           + (648).to_bytes(2, "big") + (506).to_bytes(2, "big")
           + b"\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01\xff\xd9")
    assert image_size(sof) == (506, 648)


def test_the_cover_page_is_marked_for_the_bleed():
    cover = _cover_page(_fake_png(506, 648))
    html = build_book_html([cover], title="t", page_ids=["cover"])
    assert "ripper-cover" in html
    body = build_book_html(['<html><body class="webreader"><section>'
                            '<p>text</p></section></body></html>'],
                           title="t", page_ids=["s"])
    assert "ripper-cover" not in body


def test_bleed_cover_fills_the_page_and_keeps_the_text(tmp_path):
    """A page margin lives in ``@page``, so CSS cannot bleed into it; the scan
    is painted in PDF space instead. The text underneath must survive."""
    pdf = str(tmp_path / "c.pdf")
    doc = fitz.open()
    page = doc.new_page(width=564, height=722)
    page.insert_text((60, 120), "COVER CAPTION")
    doc.save(pdf)
    doc.close()

    # A scan shaped like the page (the paper height was derived from it).
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 50, 64))
    pix.set_rect(pix.irect, (200, 60, 40))
    assert bleed_cover(pdf, pix.tobytes("png")) is True

    doc = fitz.open(pdf)
    info = doc[0].get_image_info()
    text = doc[0].get_text()
    doc.close()
    assert len(info) == 1
    bbox = fitz.Rect(info[0]["bbox"])
    assert bbox.x0 <= 1 and bbox.y0 <= 1
    assert bbox.width >= 562 and bbox.height >= 720, "the scan is painted edge to edge"
    assert "COVER CAPTION" in text.replace("\n", " ")


def test_bleed_cover_declines_without_an_image(tmp_path):
    pdf = str(tmp_path / "c.pdf")
    doc = fitz.open()
    doc.new_page(width=564, height=722)
    doc.save(pdf)
    doc.close()
    assert bleed_cover(pdf, None) is False


# --------------------------------------------------------------------------- #
# Page labels: the numbers the book actually prints
# --------------------------------------------------------------------------- #
ANCHORS = [(5, "i"), (10, "xxv"), (16, "xxvii")]


def test_labels_follow_the_anchors():
    info = pagenum.plan_labels(ANCHORS, total_pages=21)
    labels = info["labels"]
    assert labels[5][0] == "i"
    assert labels[9][0] == "v", "sheets between two anchors count up"
    assert labels[10][0] == "xxv", "the anchor itself overrules the count"
    assert labels[16][0] == "xxvii"
    assert labels[21][0] == "xxxii"
    assert [a[0] for a in info["anchors"]] == [5, 10, 16]


def test_labels_never_count_past_the_next_anchor():
    """A table that spans six sheets has two pages: the extras repeat the last
    one instead of running the numbering ahead of the book."""
    info = pagenum.plan_labels(ANCHORS, total_pages=21)
    between = [info["labels"][p][0] for p in range(10, 16)]
    assert between == ["xxv", "xxvi", "xxvi", "xxvi", "xxvi", "xxvi"]


def test_labels_do_not_invent_page_zero():
    """The front matter is counted backwards from the first anchor - and stops
    at ``i``: a sheet labelled ``0`` would be worse than a blank footer."""
    info = pagenum.plan_labels([(5, "iii")], total_pages=8)
    labels = info["labels"]
    assert labels[5][0] == "iii"
    assert labels[3][0] == "i"
    assert 2 not in labels and 1 not in labels
    assert min(v for _, _, v in labels.values()) == 1


def test_labels_fill_the_front_matter_backwards():
    info = pagenum.plan_labels([(5, "iii"), (8, "1")], total_pages=12)
    labels = info["labels"]
    assert labels[4][0] == "ii"
    assert labels[3][0] == "i"
    assert 2 not in labels, "counting stops at 1"
    assert info["segments"][0] == (2, "r", 1), "the roman run starts on sheet 3"
    assert info["segments"][-1] == (7, "D", 1)


def test_arabic_anchors_after_roman_ones_switch_style():
    info = pagenum.plan_labels([(5, "iii"), (8, "1")], total_pages=12)
    assert info["labels"][8] == ("1", "D", 1)
    assert info["labels"][7] == ("v", "r", 5)
    assert [s[1] for s in info["segments"]][-2:] == ["r", "D"]


def test_duplicate_pages_and_junk_labels_are_dropped():
    info = pagenum.plan_labels([(5, "i"), (5, "ii"), (9, ""), (12, "x1")],
                               total_pages=14)
    assert [a[0] for a in info["anchors"]] == [5]
    assert info["labels"][5][0] == "i"


def test_no_anchors_means_no_labels():
    assert pagenum.plan_labels([], total_pages=3)["labels"] == {}


@pytest.mark.parametrize("text,expected", [
    ("xxvii", 27),
    ("i", 1),
    ("xxv", 25),
    ("", None),
    ("from i to xxi", None),
    ("7", None),
])
def test_roman_value(text, expected):
    assert pagenum.roman_value(text) == expected


@pytest.mark.parametrize("text,value,style", [
    ("58", 58, "D"), ("xxvii", 27, "r"), ("", None, ""), ("Contents", None, ""),
])
def test_label_value(text, value, style):
    assert pagenum.label_value(text) == (value, style)


def test_sheets_without_a_label_are_not_stamped(tmp_path):
    """The title and copyright pages print no number in the book; inventing
    ``ii``/``iii`` there was wrong."""
    pdf = str(tmp_path / "t.pdf")
    doc = fitz.open()
    for _ in range(7):
        doc.new_page(width=564, height=722)
    doc.save(pdf)
    doc.close()

    info = pagenum.plan_labels([(5, "i")], total_pages=7)
    assert pagenum.stamp_numbers(pdf, info) == 3, "pages 5, 6, 7 only"

    doc = fitz.open(pdf)
    got = [" ".join(p.get_text().split()) for p in doc]
    doc.close()
    assert got[:4] == ["", "", "", ""]
    assert got[4:] == ["i", "ii", "iii"]


def test_page_labels_are_written_as_several_ranges(tmp_path):
    """One ``/PageLabels`` range cannot describe ``i..v`` then ``xxv..``."""
    import pikepdf

    pdf = str(tmp_path / "t.pdf")
    doc = fitz.open()
    for _ in range(12):
        doc.new_page(width=564, height=722)
    doc.save(pdf)
    doc.close()

    info = pagenum.plan_labels([(5, "i"), (8, "25")], total_pages=12)
    assert pagenum.write_page_labels(pdf, info) is True

    with pikepdf.open(pdf) as doc:
        nums = doc.Root.PageLabels.Nums
        starts = [int(nums[i]) for i in range(0, len(nums), 2)]
        styles = [str(nums[i].get("/S")) for i in range(1, len(nums), 2)]
    assert starts == [4, 7]
    assert styles == ["/r", "/D"]


def test_exercise_answers_are_opened_before_printing():
    """A closed ``<details>`` printed its "SHOW ANSWER" line and nothing else.

    One page of this edition holds nothing but answers, so it came out blank,
    the fitter dropped the empty wrapper, and the book lost page 26 entirely.
    """
    soup = _section(
        '<ol><li><details class="toggleAnswer animated">'
        '<summary class="quiz-answer-summary-button">SHOW ANSWER</summary>'
        '<p>the first and fourth questions answer this</p></details></li></ol>')
    assert soup.find("details").has_attr("open") is False
    from pdf_builder import reveal_details
    assert reveal_details(soup) == 1
    assert soup.find("details").has_attr("open") is True


def test_the_chapter_after_an_insert_keeps_its_opening_with_its_page():
    """Chapter 2 opens on page 34, but its marker sits below the introduction.

    The section right after the two R appendices therefore starts with the
    chapter title; treating that section as a page of its own printed the title
    as page 33 and pushed "34" onto the next sheet.
    """
    from pdf_builder import build_book_doc, group_pages  # noqa: F401

    pages = [
        '<html><body class="webreader"><section>'
        '<p>last page of chapter one</p>'
        '<span class="pagenumber rs_skip" epub:type="pagebreak" '
        'data-after="33" id="page-33" title="Textbook page 33"></span>'
        '<div class="block-of-pagenumbers">'
        '<span class="pagenumber rs_skip" data-after="32"></span></div>'
        '</section></body></html>',
        '<html><body class="webreader"><section>'
        '<h2>Appendix Getting Started with R and RStudio</h2>'
        '<p>In this appendix we describe how to get started with R.</p>'
        '</section></body></html>',
        '<html><body class="webreader"><section>'
        '<h1>Chapter 2. Descriptive Statistics</h1>'
        '<h2>Introduction</h2><p>After completing this chapter you will</p>'
        '<span class="pagenumber rs_skip" epub:type="pagebreak" '
        'data-after="34" id="page-34" title="Textbook page 34"></span>'
        '<p>Statistics in Practice Colgate-Palmolive Company</p>'
        '</section></body></html>',
    ]
    doc = build_book_doc(pages, stream=True,
                         new_pages=[False, True, True],
                         page_ids=["a", "b", "c"])
    groups = doc["groups"]
    opener = [g for g in groups if g["pb"] == "34"]
    assert opener, "page 34 must exist"
    assert "Chapter 2" in opener[0]["el"].get_text(), (
        "the chapter opening belongs to page 34, not to a sheet of its own")
    after = [g for g in groups if groups.index(g) > groups.index(
        [x for x in groups if x["insert"]][0]) and not g["insert"]]
    assert all(g["pb"] for g in after), (
        "every wrapper after the inserts must open a numbered page")


def test_a_shrunk_page_still_fills_the_column():
    """``zoom`` narrowed the column to 80% and centred it, wasting 20% of every
    sheet. Its own layout widening is cancelled by a max-width the reader ships,
    so the style has to release the clamp - and must *not* set a percentage
    width, which Chrome would then multiply by the zoom again."""
    from pdf_builder import zoom_style

    style = zoom_style(0.72)
    assert "zoom:0.7200" in style
    assert "max-width:none" in style
    assert "width:" not in style.replace("max-width", "")
    assert zoom_style(1.0) == ""


def test_answers_print_as_an_indented_answer_line():
    """``SHOW ANSWER`` is a button, and paper has none.

    The answer must come out as ``Answer: ...`` under its question, in one
    block, with the summary and the reader's tick removed.
    """
    from pdf_builder import format_answers

    soup = BeautifulSoup(
        '<body><ol><li><details class="toggleAnswer">'
        '<summary class="quiz-answer-summary-button"><svg></svg>SHOW ANSWER'
        '</summary><div class="summary-content-wrapper" style="display: none">'
        '<div class="correct"><p>The 10 elements are the 10 tablets.</p></div>'
        '</div></details></li></ol></body>', "lxml")
    assert format_answers(soup) == 1
    box = soup.find(class_="ripper-answer")
    assert box is not None and "ripper-answer-block" not in box["class"]
    text = " ".join(box.get_text(" ", strip=True).split())
    assert text == "Answer: The 10 elements are the 10 tablets."
    assert not soup.find("details")


def test_an_answer_that_is_a_table_keeps_its_rows():
    """Flattening a two-column answer produced "VariableMeasurementScaleCost"."""
    from pdf_builder import format_answers

    soup = BeautifulSoup(
        '<body><details class="toggleAnswer">'
        '<summary>SHOW ANSWER</summary>'
        '<div class="correct"><table><tr><td><p>Variable</p></td>'
        '<td><p>Measurement Scale</p></td></tr></table></div></details></body>',
        "lxml")
    format_answers(soup)
    box = soup.find(class_="ripper-answer")
    assert "ripper-answer-block" in box["class"]
    assert box.find("table") is not None


def test_every_page_wrapper_carries_an_invisible_index():
    """Prose search lost 703 of 1035 wrappers on the 734-page edition.

    Text matching cannot find a wrapper whose opening words are a formula image,
    a repeated "Content" heading, or a sentence the print split across sheets.
    So each wrapper carries ``G####`` (1px, transparent) inside its page block,
    which Chrome writes into the PDF text layer. It must sit *inside* the block:
    ``.book-page.ripper-newpage`` breaks before itself, and a marker in front of
    it becomes a sheet of its own.
    """
    from pdf_builder import build_book_doc, _page_snippet

    pages = [
        '<html><body class="webreader"><section><h2>Intro</h2>'
        '<p>first paragraph of the section</p>'
        '<span class="pagenumber rs_skip" epub:type="pagebreak" '
        'data-after="1"></span><p>second paragraph</p></section></body></html>',
    ]
    doc = build_book_doc(pages, stream=True, new_pages=[True], page_ids=["a"])
    els = doc["soup"].find_all(class_="ripper-page")
    assert els
    for i, el in enumerate(els):
        anchor = el.find(class_="ripper-anchor")
        assert anchor is not None, "wrapper without an index marker"
        assert anchor.get_text() == f"G{i:04d}"
        block = el.find(class_="book-page")
        assert block is not None and anchor.parent is block
    # The marker must not leak into the snippet used for footer matching.
    assert not _page_snippet(els[0]).startswith("G")


def test_a_wrapper_with_no_text_is_still_dropped():
    """The marker must not make an empty page look non-empty."""
    from pdf_builder import build_book_doc

    pages = [
        '<html><body class="webreader"><section><p>real text here</p>'
        '<span class="pagenumber rs_skip" epub:type="pagebreak" '
        'data-after="1"></span></section></body></html>',
    ]
    doc = build_book_doc(pages, stream=True, new_pages=[True], page_ids=["a"])
    for el in doc["soup"].find_all(class_="ripper-page"):
        body = el.find(class_="book-page") or el
        text = body.get_text(" ", strip=True)
        text = text.replace(el.find(class_="ripper-anchor").get_text(), "", 1)
        if not text.strip():
            assert el.find(["img", "svg", "table", "figure"]) is not None
