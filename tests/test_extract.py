"""Tests for reader_extract, driven by the synthetic SingleFile fixture.

No Cengage account or browser required - the fixture carries a synthetic
copyright-page webreader document inside #iframe-page's srcdoc, with the same
DOM shape as a saved reader page.
"""
import os
import sys

import pytest
from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from reader_extract import (  # noqa: E402
    extract_page_html_from_source,
    clean_webreader,
    page_text,
)

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "..", "fixtures_sample.html")
SHELL = os.path.join(HERE, "..", "fixtures_shell_sample.html")


@pytest.fixture
def source():
    with open(FIXTURE, encoding="utf-8") as f:
        return f.read()


def test_extract_locates_frame_and_real_content(source):
    doc = extract_page_html_from_source(source)
    assert "<html" in doc
    soup = BeautifulSoup(doc, "lxml")
    assert soup.find("body", class_="webreader") is not None
    text = page_text(doc)
    assert "Copyright Statement" in text
    assert "ISBN" in text


def test_clean_removes_scripts_but_keeps_content(source):
    doc = extract_page_html_from_source(source)
    cleaned = clean_webreader(doc)
    assert "<script" not in cleaned
    # Chrome ids live outside the frame, but the defensive strip must hold.
    assert "onetrust-consent-sdk" not in cleaned
    # Genuine textbook text survives cleaning.
    assert "Copyright Statement" in cleaned
    assert "ISBN" in cleaned


def test_extract_is_idempotent_on_already_extracted(source):
    doc = extract_page_html_from_source(source)
    again = extract_page_html_from_source(doc)
    assert "Copyright Statement" in page_text(again)


def test_shell_fixture_exposes_frame_id_without_inline_content():
    shell = open(SHELL, encoding="utf-8").read()
    soup = BeautifulSoup(shell, "lxml")
    assert soup.find(id="iframe-page") is not None
    # The Save-As shell has no inline page text (content loads dynamically),
    # so extraction correctly raises instead of returning junk.
    with pytest.raises(ValueError):
        extract_page_html_from_source(shell)
