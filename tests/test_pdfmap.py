"""Bookmark targets in a continuously printed PDF.

The book is printed as ONE document (short reader sections flow into each
other), so a section's PDF page is not its index. ``pdfmap`` recovers it from
the finished PDF's text layer; these tests pin that behaviour down without a
browser or a Cengage account.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pdfmap  # noqa: E402

fitz = pytest.importorskip("fitz", reason="PyMuPDF not installed")


def _make_pdf(path, pages):
    """pages: list of list-of-lines."""
    doc = fitz.open()
    for lines in pages:
        page = doc.new_page()
        y = 60
        for line in lines:
            page.insert_text((50, y), line, fontsize=14)
            y += 26
    doc.save(path)
    doc.close()


@pytest.fixture()
def pdf(tmp_path):
    path = str(tmp_path / "book.pdf")
    _make_pdf(path, [
        ["Chapter 1 Data", "Intro text about the chapter.", "Body line."],
        ["Body continues here.", "nothing interesting"],
        ["1.1 Applications", "Text of the section.", "More text."],
        ["Exercises", "1. Do this.", "2. Do that."],
    ])
    return path


def test_locates_each_section_at_its_heading(pdf):
    metas = [{"title": "Chapter 1 Data"},
             {"title": "1.1 Applications"},
             {"title": "Exercises"}]
    assert pdfmap.locate_section_pages(pdf, metas) == [1, 3, 4]


def test_a_mention_in_running_text_does_not_steal_the_bookmark(tmp_path):
    """The real failure mode: 'see Exercises' inside a body paragraph."""
    path = str(tmp_path / "mention.pdf")
    _make_pdf(path, [
        ["Chapter 1 Data"],
        ["Please complete all of the exercises at the end of this chapter "
         "before continuing, because they build on everything above and are "
         "graded as part of the course requirement for the whole term."],
        ["Exercises", "1. Do this."],
    ])
    metas = [{"title": "Chapter 1 Data"}, {"title": "Exercises"}]
    assert pdfmap.locate_section_pages(path, metas) == [1, 3]


def test_duplicate_titles_resolve_in_reading_order(pdf):
    metas = [{"title": "Chapter 1 Data"}, {"title": "Exercises"},
             {"title": "Exercises"}]
    pages = pdfmap.locate_section_pages(pdf, metas)
    assert pages == sorted(pages)
    assert pages[-1] == 4


def test_missing_heading_keeps_the_previous_page(pdf):
    metas = [{"title": "Chapter 1 Data"}, {"title": "Glossary of Nothing"}]
    assert pdfmap.locate_section_pages(pdf, metas) == [1, 1]


def test_needles_ignore_very_short_and_duplicate_titles():
    assert pdfmap.needles({"title": "1"}) == []
    assert pdfmap.needles({"title": "Exercises", "heading": "Exercises"}) == \
        ["exercises"]


def test_estimate_spreads_sections_over_the_book():
    metas = [{"title": "a"}, {"title": "b"}, {"title": "c"}, {"title": "d"}]
    pages = pdfmap.estimate_section_pages(metas, total_pages=40)
    assert pages[0] == 1
    assert pages == sorted(pages)
    assert max(pages) <= 40


def test_apply_pages_writes_pdf_page():
    metas = [{"title": "a"}, {"title": "b"}]
    pdfmap.apply_pages(metas, [3, 9])
    assert [m["pdf_page"] for m in metas] == [3, 9]
