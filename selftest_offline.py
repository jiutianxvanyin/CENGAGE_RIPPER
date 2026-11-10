"""Offline end-to-end self-test - no Cengage account, no Selenium.

Takes one saved reader page (the synthetic SingleFile fixture by default), clones it
into a few synthetic "pages" with different titles, then runs the SAME assembly
path the real tool uses (``cengage_ripper.assemble``) to prove that:

  * Chrome prints real text (not images),
  * per-page PDFs merge in order,
  * PDF bookmarks come out of the page titles in the right nesting.

Usage:
    python selftest_offline.py [--input SOME_SAVED_PAGE.html] [--keep]
"""
from __future__ import annotations

import os
import shutil
import sys

from bs4 import BeautifulSoup

import toc as toc_mod
from cengage_ripper import assemble, save_index
from config import DEFAULTS
from reader_extract import clean_webreader, extract_page_html_from_source

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "fixtures_sample.html")
WORK = os.path.join(HERE, "_selftest")

# (title, heading level, keep same body as the real page)
SYNTHETIC_PAGES = [
    ("Chapter 1: The Basics", 1),
    ("Chapter 1: The Basics", 1),   # same title -> must collapse into one bookmark
    ("1.1 Reading Data", 2),
    ("Chapter 2: Regression", 1),
]


def retitle(page_html: str, title: str, cgi_suffix: str, page_number: int) -> str:
    """Give the page a new <title> AND matching first heading.

    Bookmarks are located by finding the heading in the finished PDF's text
    layer, so a page whose heading was never renamed could not be located.
    A pagebreak marker is planted mid-way through the text so the run also
    exercises "one textbook page = one PDF page".
    """
    soup = BeautifulSoup(page_html, "lxml")
    node = soup.find("title")
    if node is not None:
        node.string = title
    body = soup.find("body")
    if body is not None:
        body["data-cgi"] = f"SELFTEST{cgi_suffix}"
    heading = soup.find(["h1", "h2", "h3"])
    if heading is not None:
        heading.string = title
    paragraphs = soup.find_all("p")
    if len(paragraphs) > 1:
        target = paragraphs[len(paragraphs) // 2]
        mark = soup.new_tag("span")
        mark["class"] = ["ripper-pb"]
        mark["data-pb"] = str(page_number)
        target.insert_before(mark)
        # Unique words right after the break: the clones below share their body
        # text, so without this every page break would be found on page 1.
        tag = soup.new_tag("p")
        tag.string = (f"SELFTESTMARK{page_number} opens textbook page "
                      f"{page_number} in this self-test copy.")
        mark.insert_after(tag)
    return str(soup)


def main() -> int:
    source = FIXTURE
    if "--input" in sys.argv:
        source = sys.argv[sys.argv.index("--input") + 1]

    print(f"reading    : {os.path.basename(source)}")
    with open(source, encoding="utf-8") as f:
        base_page = clean_webreader(extract_page_html_from_source(f.read()))

    if os.path.isdir(WORK):
        shutil.rmtree(WORK)
    pages_dir = os.path.join(WORK, "pages")
    os.makedirs(pages_dir)

    cfg = dict(DEFAULTS)
    cfg.update({
        "pages_dir": pages_dir,
        "output_pdf": os.path.join(WORK, "book.pdf"),
        "output_html": os.path.join(WORK, "book.html"),
        "debug_dir": None,
        "toc": True,
        "title": "Offline Self-Test Book",
    })

    metas: list[dict] = []
    for index, (title, level) in enumerate(SYNTHETIC_PAGES, start=1):
        page = retitle(base_page, title, f"{index:02d}", page_number=10 + index)
        name = f"page_{index:04d}.html"
        with open(os.path.join(pages_dir, name), "w", encoding="utf-8") as f:
            f.write(page)
        meta = toc_mod.page_meta(page, page_number=str(10 + index))
        meta.update({"page": index, "file": name,
                     "page_size": [900, 1600],   # what the live frame would report
                     "sig": ["cgi", f"SELFTEST{index:02d}"]})
        metas.append(meta)
        print(f"  staged   : {name}  {meta['title']!r}  level={meta['level']}")

    save_index(cfg, metas)

    print("\nrunning the real assembly path (Chrome print -> merge -> bookmarks)")
    assemble(cfg, metas)

    # --- verification -----------------------------------------------------
    import pikepdf

    ok = True
    if not os.path.exists(cfg["output_pdf"]):
        print("FAIL: no PDF produced")
        return 1

    with pikepdf.open(cfg["output_pdf"]) as pdf:
        n_pages = len(pdf.pages)
        with pdf.open_outline() as outline:
            tops = list(outline.root)
            titles = [t.title for t in tops]
            subs = [c.title for t in tops for c in t.children]
        doc_title = str(pdf.docinfo.get("/Title", ""))

    print(f"\nPDF pages : {n_pages}")
    print(f"PDF title : {doc_title!r}")
    print(f"bookmarks : {titles} + {subs}")
    print(f"size      : {os.path.getsize(cfg['output_pdf'])} bytes")

    expected = ["Chapter 1: The Basics", "Chapter 2: Regression"]
    if titles != expected:
        print(f"FAIL: expected top-level {expected}")
        ok = False
    if subs != ["1.1 Reading Data"]:
        print('FAIL: expected nested ["1.1 Reading Data"]')
        ok = False
    if n_pages < 2:
        print(f"FAIL: {n_pages} PDF page(s) for 4 pages of text")
        ok = False

    # The text must be real and searchable, not a picture of text.
    try:
        import fitz
        with fitz.open(cfg["output_pdf"]) as doc:
            texts = [p.get_text("text") for p in doc]
        chars = sum(len(t.strip()) for t in texts)
        print(f"text layer: {chars} chars over {len(texts)} pages")
        if chars < 500:
            print("FAIL: the PDF has almost no extractable text")
            ok = False
        for entry in toc_mod.build_toc(metas, max_level=3):
            page = int(entry.get("pdf_page") or 1)
            want = entry["title"].lower()[:18]
            if want not in " ".join(texts[page - 1].lower().split()):
                print(f"FAIL: bookmark {entry['title']!r} -> page {page}, "
                      f"but that page does not contain it")
                ok = False

        # Textbook page numbers, planted as 10/20/30/40, must reach the footer.
        wanted = {str(10 + i) for i in range(1, len(SYNTHETIC_PAGES) + 1)}
        seen = set()
        for text in texts:
            tail = text.strip().split()
            if tail and tail[-1].strip("()") in wanted:
                seen.add(tail[-1].strip("()"))
            elif tail and tail[-1] == "(cont.)" and len(tail) > 1:
                seen.add(tail[-2])
        print(f"page numbers: stamped {sorted(seen)} in the footer")
        if not wanted & seen:
            print("FAIL: no textbook page number reached the footer")
            ok = False
    except ImportError:
        print("note: PyMuPDF missing -> skipped text-layer / bookmark checks")

    if ok:
        print("\nOK - real text, sections flow into a book, bookmarks point at "
              "their own page")
        print(f"open it: {cfg['output_pdf']}")
        if "--keep" not in sys.argv:
            shutil.rmtree(WORK, ignore_errors=True)
            print("(work dir removed; re-run with --keep to inspect it)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
