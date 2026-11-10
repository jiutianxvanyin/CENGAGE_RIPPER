"""Tests for toc: page metadata harvesting, outline shaping, PDF bookmarks."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from toc import (  # noqa: E402
    apply_outline,
    build_toc,
    nest,
    normalize_levels,
    numbering_level,
    page_meta,
)
from reader_extract import clean_webreader, extract_page_html_from_source  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "..", "fixtures_sample.html")


@pytest.fixture
def doc():
    with open(FIXTURE, encoding="utf-8") as f:
        return clean_webreader(extract_page_html_from_source(f.read()))


def test_page_meta_reads_title_cgi_and_level(doc):
    meta = page_meta(doc, page_number="iv")
    assert meta["title"] == "Copyright Statement"
    assert meta["heading"] == "Copyright Statement"
    assert meta["level"] == 1
    assert meta["epub_type"] == "frontmatter"
    assert meta["cgi"], "body[data-cgi] must be harvested as the page identity"
    assert meta["page_label"] == "iv"


@pytest.mark.parametrize("title,expected", [
    ("1.1 Reading Data", 2),
    ("1.2.3 A Deep Section", 3),
    ("Chapter 4: Probability", 1),
    ("Unit 7 - Sampling", 1),
    ("12 Exercises", 1),
    ("Copyright Statement", 0),
    ("2024 Cengage Learning", 0),
    ("", 0),
])
def test_numbering_level(title, expected):
    assert numbering_level(title) == expected


def test_page_meta_uses_numbering_when_heading_is_flat(doc):
    # A sub-section shipped as <h1> must still land at level 2 via its number.
    flat = doc.replace("<title>Copyright Statement</title>", "<title>1.1 Reading Data</title>")
    assert page_meta(flat)["level"] == 2


def test_build_toc_collapses_repeated_titles():
    metas = [
        {"page": 1, "title": "Chapter 1", "level": 1},
        {"page": 2, "title": "Chapter 1", "level": 1},
        {"page": 3, "title": "1.1 Intro", "level": 2},
    ]
    entries = build_toc(metas)
    assert [e["title"] for e in entries] == ["Chapter 1", "1.1 Intro"]
    assert entries[0]["page"] == 1
    assert entries[1]["level"] == 2


def test_build_toc_per_page_keeps_every_page():
    metas = [{"page": i, "title": "Same", "level": 1} for i in range(1, 4)]
    assert len(build_toc(metas, per_page=True)) == 3


def test_build_toc_falls_back_when_title_missing():
    entries = build_toc([{"page": 7, "title": "", "heading": "", "level": 1}])
    assert entries[0]["title"] == "Page 7"


def test_normalize_levels_fixes_first_entry_and_jumps():
    entries = [
        {"title": "A", "level": 3},
        {"title": "B", "level": 3},
        {"title": "C", "level": 1},
    ]
    out = normalize_levels(entries)
    assert out[0]["level"] == 1          # first entry is promoted
    assert out[1]["level"] == 2          # no jump deeper than one level
    assert out[2]["level"] == 1


def test_nest_builds_children_tree():
    entries = normalize_levels([
        {"title": "Part I", "level": 1},
        {"title": "Ch 1", "level": 2},
        {"title": "1.1", "level": 3},
        {"title": "Ch 2", "level": 2},
        {"title": "Part II", "level": 1},
    ])
    tree = nest(entries)
    assert [n["title"] for n in tree] == ["Part I", "Part II"]
    assert [n["title"] for n in tree[0]["children"]] == ["Ch 1", "Ch 2"]
    assert tree[0]["children"][0]["children"][0]["title"] == "1.1"


def _make_pdf(path, n_pages):
    """Write a valid n-page blank PDF (real xref) for outline testing."""
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", None]
    kids = " ".join(f"{3 + i} 0 R" for i in range(n_pages))
    objs[1] = f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>".encode()
    for i in range(n_pages):
        objs.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Contents {3 + n_pages + i} 0 R /Resources << >> >>".encode())
    for _ in range(n_pages):
        objs.append(b"<< /Length 0 >>\nstream\nendstream")

    out = b"%PDF-1.4\n"
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 " + str(len(objs) + 1).encode() + b"\n0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += b"trailer\n<< /Size " + str(len(objs) + 1).encode() + b" /Root 1 0 R >>\n"
    out += b"startxref\n" + str(xref).encode() + b"\n%%EOF"
    with open(path, "wb") as f:
        f.write(out)


def test_apply_outline_writes_nested_bookmarks():
    pikepdf = pytest.importorskip("pikepdf")
    path = os.path.join(HERE, "_tmp_outline.pdf")
    try:
        _make_pdf(path, 6)
        entries = normalize_levels([
            {"title": "Part I", "level": 1, "page": 1, "pdf_page": 1},
            {"title": "Ch 1", "level": 2, "page": 2, "pdf_page": 2},
            {"title": "1.1 Deep", "level": 3, "page": 3, "pdf_page": 3},
            {"title": "Part II", "level": 1, "page": 4, "pdf_page": 4},
        ])
        assert apply_outline(path, entries, book_title="My Book") == 2

        with pikepdf.open(path) as pdf:
            assert len(pdf.pages) == 6
            assert str(pdf.docinfo.get("/Title", "")) == "My Book"
            with pdf.open_outline() as outline:
                tops = list(outline.root)
                assert [t.title for t in tops] == ["Part I", "Part II"]
                assert [c.title for c in tops[0].children] == ["Ch 1"]
                assert [g.title for g in tops[0].children[0].children] == ["1.1 Deep"]
    finally:
        if os.path.exists(path):
            os.remove(path)


def test_apply_outline_is_noop_without_entries():
    assert apply_outline("does-not-matter.pdf", []) == 0
