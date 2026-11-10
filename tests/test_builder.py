"""Tests for pdf_builder: book assembly, PDF merge, and (optional) Chrome print."""
import os
import sys
import shutil

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pdf_builder import (  # noqa: E402
    build_book_html,
    merge_pdfs,
    print_css,
    write_page_html,
)

HERE = os.path.dirname(os.path.abspath(__file__))

PAGE = (
    '<html xmlns:epub="http://www.idpf.org/2007/ops">'
    '<head><style>.x{color:red}</style>'
    '<link rel="stylesheet" href="https://example.com/a.css"></head>'
    '<body class="webreader"><section><h1>Title</h1><p>Body text</p></section>'
    "</body></html>"
)


def test_build_book_html_paginates_and_keeps_style():
    out = build_book_html([PAGE, PAGE], title="Test")
    assert out.count('class="webreader book-page"') == 2
    assert "break-after: page" in out
    assert "Title" in out and "Body text" in out
    # Styles and stylesheet link are collected into the head.
    assert ".x{color:red}" in out
    assert "example.com/a.css" in out


def test_stream_mode_lets_sections_flow_into_each_other():
    out = build_book_html([PAGE, PAGE], title="Test", stream=True)
    assert out.count('class="webreader book-page"') == 2
    assert ".book-page { break-after: auto; }" in out
    assert "break-after: avoid-page" in out      # headings stay with their text


def test_book_paper_margins_repeat_on_every_printed_page():
    """@page margins (not body padding) - body padding existed only once, so
    continuation pages had their first line glued to the top edge."""
    css = print_css(book=(752, 960, 16))
    assert "@page { size: 752px 960px; margin: 12px 16px 12px; }" in css
    assert "body { margin: 0 !important; padding: 0 !important; }" in css


def test_book_paper_accepts_a_footer_margin():
    css = print_css(book=(752, 1500, 18, 14, 30))
    assert "@page { size: 752px 1500px; margin: 14px 18px 30px; }" in css


def test_legacy_paper_sizes_still_work():
    assert "@page { size: 900px 1600px; margin: 0; }" in print_css((900, 1600))
    assert "@page { size: A4" in print_css(None)


def test_write_page_html_injects_base_when_given():
    path = os.path.join(HERE, "_tmp_page.html")
    try:
        write_page_html(PAGE, path, base_url="https://reader.example/book")
        content = open(path, encoding="utf-8").read()
        assert 'href="https://reader.example/book"' in content
    finally:
        if os.path.exists(path):
            os.remove(path)


def _make_minimal_pdf(path):
    """Write a valid single blank-page PDF (with xref) for merge testing."""
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << >> >>",
        b"<< /Length 0 >>\nstream\nendstream",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_pos = len(out)
    out += b"xref\n0 5\n0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += b"trailer\n<< /Size 5 /Root 1 0 R >>\nstartxref\n"
    out += str(xref_pos).encode() + b"\n%%EOF"
    with open(path, "wb") as f:
        f.write(out)


def test_merge_pdfs_concatenates_page_order():
    pytest.importorskip("pikepdf")
    a = os.path.join(HERE, "_tmp_a.pdf")
    b = os.path.join(HERE, "_tmp_b.pdf")
    merged = os.path.join(HERE, "_tmp_merged.pdf")
    try:
        _make_minimal_pdf(a)
        _make_minimal_pdf(b)
        merge_pdfs([a, b], merged)
        import pikepdf
        with pikepdf.open(merged) as pdf:
            assert len(pdf.pages) == 2
    finally:
        for p in (a, b, merged):
            if os.path.exists(p):
                os.remove(p)


def test_html_to_pdf_runs_when_chrome_available():
    chrome = shutil.which("chrome") or shutil.which("google-chrome")
    if not chrome:
        pytest.skip("Chrome not installed in this environment")
    html_path = os.path.join(HERE, "_tmp_in.html")
    pdf_path = os.path.join(HERE, "_tmp_out.pdf")
    try:
        open(html_path, "w", encoding="utf-8").write(
            "<!DOCTYPE html><html><body><h1>Hi</h1></body></html>")
        html_to_pdf(html_path, pdf_path, chrome)
        with open(pdf_path, "rb") as f:
            assert f.read(5) == b"%PDF-"
    finally:
        for p in (html_path, pdf_path):
            if os.path.exists(p):
                os.remove(p)
