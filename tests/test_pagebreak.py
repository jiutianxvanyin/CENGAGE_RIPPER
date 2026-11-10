"""Page numbers, page-break markers and in-book links.

The reader marks where each textbook page begins (``epub:type="pagebreak"``).
Those markers are what lets the PDF keep the book's own numbering, so they get
their own test file: extraction must keep them, the builder must turn them into
real page breaks, and ``pagenum`` must map book page -> PDF page without drift.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fitz                                      # noqa: E402
import pagenum                                   # noqa: E402
from pdf_builder import build_book_html, paginate  # noqa: E402
from reader_extract import (                     # noqa: E402
    PB_CLASS,
    clean_webreader,
    pagebreak_numbers,
)

PAGE_WITH_BREAK = """
<html xmlns:epub="http://www.idpf.org/2007/ops"><head><title>t</title></head>
<body class="webreader"><section>
  <p>first paragraph that ends on the previous printed page</p>
  <p>The sentence starts here
     <span class="pagenumber rs_skip" data-after="58" epub:type="pagebreak"
           id="page-58" aria-label="page 58"></span>
     and continues on the next printed page, for a while.</p>
  <p>a later paragraph</p>
</section></body></html>
"""


def test_clean_keeps_pagebreak_markers():
    cleaned = clean_webreader(PAGE_WITH_BREAK)
    assert PB_CLASS in cleaned
    assert 'data-pb="58"' in cleaned
    assert 'id="page-58"' in cleaned            # still a link target
    assert "rs_skip" not in cleaned             # the noise filter still runs
    assert pagebreak_numbers(cleaned) == [58]


def test_paginate_splits_the_paragraph_at_the_marker():
    breaks = []
    html = build_book_html([PAGE_WITH_BREAK], title="t", breaks=breaks,
                           page_ids=["sec-a"])
    assert [b["num"] for b in breaks] == [58]
    # The break became a block, so Chrome can act on it.
    assert '<div class="ripper-pb" data-pb="58"' in html
    # The paragraph was cut in two: text before and after the marker.
    assert "The sentence starts here" in html
    assert "and continues on the next printed page" in html
    assert html.index("The sentence starts here") < html.index("ripper-pb\"")


def test_paginate_handles_a_marker_between_blocks():
    html = ('<html><body class="webreader"><section>'
            '<h2>Heading</h2>'
            '<span class="ripper-pb" data-pb="9"></span>'
            '<p>body</p></section></body></html>')
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    breaks = paginate(soup)
    assert [b["num"] for b in breaks] == [9]
    assert "body" in str(soup)
    assert str(soup).index("ripper-pb") < str(soup).index("<p>body")


# --------------------------------------------------------------------------- #
# Links
# --------------------------------------------------------------------------- #
LINK_PAGE = """
<html><head><title>t</title></head><body class="webreader"><section>
  <p>
    <a class="gt-internal-link" data-page-id="content-bd_ch_01_sect_02"
       href="https://ebooks.cengage.com/reader/x/content-bd_ch_01_sect_02?code=1#TBL1">
       Table 1.1</a>
    <a data-page-id="content-bd_ch_01_sect_02"
       href="https://ebooks.cengage.com/reader/x/content-bd_ch_01_sect_02?code=1">1.2</a>
    <a href="https://ebooks.cengage.com/reader/x/menu-search?q=x">search</a>
    <a href="https://example.org/paper.pdf">external</a>
  </p>
  <figure id="TBL1">a table</figure>
</section></body></html>
"""


def test_links_become_in_document_jumps():
    stats = {}
    html = build_book_html([LINK_PAGE], title="t", page_ids=["bd_ch_01_sect_02"],
                           link_stats=stats)
    assert 'href="#TBL1"' in html                       # figure stays a fragment
    assert 'href="#sec-bd_ch_01_sect_02"' in html       # section anchor
    assert "menu-search" not in html                    # web-only link unlinked
    assert "example.org/paper.pdf" in html              # external link untouched
    assert 'target="_parent"' not in html
    assert stats["internal"] >= 2 and stats["dropped"] == 1


# --------------------------------------------------------------------------- #
# Mapping book page -> PDF page
# --------------------------------------------------------------------------- #
def _breaks(*nums):
    return [{"num": n, "snippet": f"text of page {n}"} for n in nums]


def test_plan_one_to_one():
    info = pagenum.plan(_breaks(5, 6, 7), [2, 3, 4], total_pages=4)
    assert info["perfect"] is True
    assert info["front_pages"] == 1
    assert info["body_start_number"] == 5
    assert [e["pdf_page"] for e in info["map"]] == [2, 3, 4]
    assert pagenum.book_page_of_pdf(info, 3) == 6
    assert pagenum.book_page_of_pdf(info, 1) is None      # front matter


def test_plan_merges_markers_that_share_a_page():
    """The reader cannot put a marker inside a table, so it parks two together."""
    info = pagenum.plan(_breaks(5, 6, 8), [2, 2, 3], total_pages=3)
    assert info["merged"] == [6]
    assert info["perfect"] is False
    # The hoisted marker explains the gap (the table is there, it is just not
    # page-aligned), so nothing is padded: no blank pages are invented.
    assert info["inserts"] == 0
    assert [e["pdf_page"] for e in info["map"]] == [2, 3]
    assert pagenum.book_page_of_pdf(info, 2) == 5
    assert pagenum.book_page_of_pdf(info, 3) == 8


def test_plan_flags_a_page_that_spills_over_two_sheets():
    info = pagenum.plan(_breaks(5, 6), [2, 4], total_pages=6)
    assert any("spilled" in w for w in info["warnings"])
    assert info["perfect"] is False
    assert pagenum.segment_of(info, 3) == (5, True)      # continuation
    assert pagenum.segment_of(info, 4) == (6, False)


def test_plan_front_matter_is_not_counted_as_missing():
    info = pagenum.plan(_breaks(1, 2), [4, 5], total_pages=5)
    assert info["front_pages"] == 3
    assert info["inserts"] == 0
    assert info["warnings"] == []


# --------------------------------------------------------------------------- #
# Markers the reader could not place
# --------------------------------------------------------------------------- #
PARKED_RUN = """
<html xmlns:epub="http://www.idpf.org/2007/ops"><head><title>About</title></head>
<body class="webreader"><section>
  <h1>About the Authors</h1>
  <div class="block-of-pagenumbers">
    <span class="pagenumber rs_skip" data-after="from&#10;i&#10;to&#10;xxi"
          epub:type="pagebreak" id="page-i"></span>
    <span class="pagenumber rs_skip" data-after="ii" epub:type="pagebreak"
          id="page-ii"></span>
    <span class="pagenumber rs_skip" data-after="iii" epub:type="pagebreak"
          id="page-iii"></span>
  </div>
  <p>Professor Cochran has published over 45 papers.</p>
</section></body></html>
"""


def test_a_parked_run_of_markers_costs_one_page_break_not_many():
    """The reader parks a whole run of page numbers in one block when it cannot
    place them; each one used to become a forced page break, which printed a
    stack of blank sheets (10 of them in the real front matter)."""
    breaks = []
    html = build_book_html([PARKED_RUN], title="t", breaks=breaks,
                           page_ids=["sec-about"])
    real = [b for b in breaks if not b["phantom"]]
    parked = [b for b in breaks if b["phantom"]]
    assert len(real) == 1, "only the marker with text after it starts a page"
    assert len(parked) == 2, "i and ii are parked; iii owns the paragraph below"
    assert [b["label"] for b in breaks] == ["from\ni\nto\nxxi", "ii", "iii"], \
        "document order is preserved even though iii was moved out of the block"
    assert html.count('<div class="ripper-pb"') == 1
    assert 'id="page-i"' in html, "the anchors survive for in-book links"


def test_a_marker_before_only_an_image_still_starts_a_page():
    html_in = ('<html><body class="webreader"><section><p>before</p>'
               '<span class="ripper-pb" data-pb="9" id="page-9"></span>'
               '<figure><img src="data:image/png;base64,AAA"/></figure>'
               '</section></body></html>')
    breaks = []
    build_book_html([html_in], title="t", breaks=breaks, page_ids=["s"])
    assert [b["phantom"] for b in breaks] == [False]


def test_plan_does_not_pad_pages_that_have_no_content():
    """A parked page is absent from the capture but not *missing*: padding it
    would shift every later page number by one."""
    breaks = [{"num": 1, "snippet": "a"}, {"num": 5, "snippet": "b"}]
    located = [1, 2]
    plain = pagenum.plan(breaks, located, 2)
    assert plain["inserts"] == 3, "sanity: 2..4 look missing without an explanation"
    skipped = pagenum.plan(breaks, located, 2, skip=[2, 3, 4])
    assert skipped["inserts"] == 0
    assert skipped["map"][1]["pdf_page"] == 2


# --------------------------------------------------------------------------- #
# The printed footer
# --------------------------------------------------------------------------- #
def test_roman_conversion():
    assert pagenum.roman(1) == "i"
    assert pagenum.roman(4) == "iv"
    assert pagenum.roman(24) == "xxiv"
    assert pagenum.roman(40) == "xl"
    assert pagenum.roman(0) == ""


def test_front_matter_gets_sheet_numbers_and_body_does_not():
    info = {"front_pages": 2}
    assert pagenum.sheet_label(info, 1) == "i"
    assert pagenum.sheet_label(info, 2) == "ii"
    assert pagenum.sheet_label(info, 3) == "", "body pages use the book number"


def _blank_pdf(path, pages=2):
    doc = fitz.open()
    for _ in range(pages):
        doc.new_page(width=564, height=1125)
    doc.save(path)
    doc.close()


def test_every_page_gets_a_visible_footer_number(tmp_path):
    """Front matter used to print nothing at all, so a 16-page file had no
    numbering to read; body pages were 8.5pt grey on a 15.6in sheet, which
    looked empty once the viewer fitted the page to the screen."""
    pdf = str(tmp_path / "t.pdf")
    _blank_pdf(pdf, pages=3)
    info = {"front_pages": 1,
            "map": [{"pdf_page": 2, "book_page": 41, "pdf_span": 2,
                     "label": "41"}]}
    assert pagenum.stamp_numbers(pdf, info) == 3

    doc = fitz.open(pdf)
    footers = []
    for page in doc:
        spans = [s for b in page.get_text("dict")["blocks"]
                 for line in b.get("lines", []) for s in line["spans"]
                 if s["bbox"][3] > page.rect.height - 40]
        footers.append([(s["text"], round(s["size"], 1), s["font"]) for s in spans])
    doc.close()

    assert footers[0][0][0] == "i", "front matter prints its roman sheet number"
    assert footers[1][0][0] == "41"
    assert footers[2][0][0] == "41 (cont.)", "a spilled page says so"
    size = footers[1][0][1]
    assert size >= 12, f"{size}pt on a 15.6in sheet is a speck"
    assert "Bold" in footers[1][0][2]


def test_front_matter_numbers_are_framed_with_rules(tmp_path):
    """A roman 'i' is a hairline; the drawn rules are what make it findable."""
    pdf = str(tmp_path / "t.pdf")
    _blank_pdf(pdf, pages=1)
    pagenum.stamp_numbers(pdf, {"front_pages": 1})
    doc = fitz.open(pdf)
    rules = [d for d in doc[0].get_drawings() if d.get("rect")
             and d["rect"].height < 3 and d["rect"].width < 20]
    doc.close()
    assert len(rules) == 2, "one rule on each side"
    assert len({round(r["rect"].y0, 1) for r in rules}) == 1, "they line up"


def test_a_capture_without_markers_is_numbered_by_sheet(tmp_path):
    pdf = str(tmp_path / "t.pdf")
    _blank_pdf(pdf, pages=2)
    assert pagenum.stamp_numbers(pdf, {"sheet_only": True}) == 2
    doc = fitz.open(pdf)
    got = [" ".join(p.get_text().split()) for p in doc]
    doc.close()
    assert got == ["1", "2"]
