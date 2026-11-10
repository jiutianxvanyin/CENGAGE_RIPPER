"""Configuration loading: defaults -> config.json -> CLI overrides."""
from __future__ import annotations

import argparse
import json
import os

DEFAULTS = {
    "browser": "chrome",
    "chrome_path": None,
    "headless": False,
    # Persistent Chrome profile: the Cengage session lives here, so you log in
    # once and never again. Set to None for a throwaway profile.
    "user_data_dir": "chrome-profile",
    # Port used for the DevTools connection. The tool reuses whatever Chrome is
    # already listening there (that is how the login survives), and only starts
    # a new browser when nothing answers.
    "browser_port": 9222,
    # >0 = attach ONLY to that port, never launch a browser
    "attach_port": 0,
    # Close the capture browser when the run ends. Default False: the browser
    # stays open so the next run is a bare ENTER with the login intact.
    "close_browser": False,
    "launch_timeout": 45,       # max wait for a freshly launched Chrome
    "page_wait": 1.2,           # settle time after clicking next
    "poll_interval": 0.3,       # how often to poll for the page to change
    "advance_timeout": 15,      # max wait for the page identity to change
    "frame_timeout": 20,        # max wait for #iframe-page to appear
    "login_timeout": 600,       # max wait for manual login in --auto mode
    "page_load_timeout": 45,    # seconds; stops frame switches blocking forever
    "script_timeout": 30,
    "auto_start": False,        # skip the "press ENTER" prompt
    "resume": True,             # continue a previous capture instead of redoing it
    "max_pages": 0,             # 0 = no limit
    # Everything the tool produces lands in this folder (relative to the working
    # directory), so the PDF is never lost next to the source files.
    "output_dir": "output",
    "output_pdf": "output/textbook.pdf",
    "output_html": None,        # optional self-contained HTML export
    "open_pdf": False,          # open the PDF in the default viewer when done
    "pages_dir": "pages",
    "debug_dir": "debug",       # per-page raw HTML dumps (selector debugging)
    "diagnose_dir": "diagnose",  # frame/DOM report written when a run fails
    "debug": False,             # alias for --debug (forces debug_dir)
    "fast": False,              # legacy alias: skip bookmarks, one quick pass
    # False (default) = print the whole book as ONE continuous document and
    # recover bookmark targets from the PDF text. True = print each reader page
    # alone: exact page numbers, but every short section leaves a white tail.
    "split_print": False,
    "toc": True,                # write PDF bookmarks from page titles / data-cgi
    "inline_assets": True,      # turn page images into data: URIs (figures survive)
    "toc_per_page": False,      # False = collapse consecutive pages sharing a title
    "toc_max_level": 3,         # deepest heading level kept in the outline
    # "book" = PDF page is the text column + a small gutter (default, no wasted
    # margins); "auto" = the measured reader frame; "a4" = plain A4.
    "paper": "book",
    # [width, height, side gutter, top margin, bottom margin] in px. The body
    # text column stays 720px wide (the EPUB's own max-width), so only the
    # height decides how much text fits on one page. The height is a starting
    # point: printing re-runs with a taller page until every textbook page fits
    # in exactly one PDF page (that is what binds the two page numbers).
    "book_page": [752, 963, 16, 12, 26],
    # Take the paper's shape from the cover scan: the cover is exactly one page,
    # so its pixel ratio is the book's. "cover" = follow the image, anything
    # else = use the book_page height as given.
    "paper_ratio": "cover",
    # Paint the cover over the whole first sheet (CSS cannot bleed into the
    # @page margin, and a one-page-tall image would be pushed to sheet 2).
    "cover_bleed": True,
    # Stamp the textbook page number in the bottom margin and write /PageLabels.
    "page_numbers": True,
    # Jump to the first page of the book before capturing, so the PDF really
    # starts at the cover instead of whatever page happened to be on screen.
    "from_start": True,
    # Re-walk the book and overwrite pages already on disk (needed after the
    # extraction changed - resume would keep the old files).
    "refresh": False,
    # Stop before the first reader page whose title (or reader URL) contains
    # this text. Empty = capture to the end. Useful to try one part of a book
    # ("--until 'Chapter 1'") without walking the whole thing.
    "until": "",
    "rebuild": False,           # re-print from pages/ without a browser
    "title": "eTextbook",
}

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception:
            pass

    p = argparse.ArgumentParser(
        description="Capture a Cengage eTextbook into a bookmarked PDF.")
    p.add_argument("-o", "--output", dest="output_pdf", help="output PDF path")
    p.add_argument("--output-dir", dest="output_dir",
                   help="folder for the PDF/HTML (default: ./output)")
    p.add_argument("--open", action="store_true", dest="open_pdf",
                   help="open the finished PDF in your default viewer")
    p.add_argument("--html", dest="output_html", help="also export one HTML book")
    p.add_argument("--headless", action="store_true", help="run Chrome headless")
    p.add_argument("--fast", action="store_true",
                   help="skip bookmarks (the print is already a single pass)")
    p.add_argument("--split-print", action="store_true", dest="split_print",
                   help="print each reader page on its own sheet: exact "
                        "bookmark pages, but a white tail after short sections")
    p.add_argument("--max-pages", type=int, dest="max_pages")
    p.add_argument("--pages-dir", dest="pages_dir")
    p.add_argument("--title", dest="title", help="book title / PDF metadata")
    p.add_argument("--auto", action="store_true", dest="auto_start",
                   help="don't wait for ENTER; poll for the reader instead")
    p.add_argument("--fresh", action="store_true",
                   help="start over (previous capture is moved aside, not deleted)")
    p.add_argument("--rebuild", action="store_true",
                   help="re-print and re-bookmark from pages/ without a browser")
    p.add_argument("--no-toc", action="store_true", help="skip PDF bookmarks")
    p.add_argument("--no-inline-assets", action="store_true",
                   help="do not embed page images (faster, but figures may be "
                        "missing from the PDF)")
    p.add_argument("--toc-per-page", action="store_true",
                   help="one bookmark per page instead of one per title")
    p.add_argument("--no-resume", action="store_true", help="disable resume")
    p.add_argument("--debug", action="store_true",
                   help="dump every page's raw HTML and a DOM report on failure")
    p.add_argument("--attach", nargs="?", type=int, const=9222, dest="attach_port",
                   metavar="PORT",
                   help="only connect to a Chrome already listening on PORT "
                        "(never launch one); default port 9222")
    p.add_argument("--port", type=int, dest="browser_port", metavar="PORT",
                   help="DevTools port to reuse/launch on (default 9222)")
    p.add_argument("--close-browser", action="store_true", dest="close_browser",
                   help="close the capture browser when the run ends "
                        "(default: leave it open for the next run)")
    p.add_argument("--profile", dest="user_data_dir",
                   help="persistent Chrome profile dir (default: ./chrome-profile)")
    p.add_argument("--no-profile", action="store_true",
                   help="throwaway profile (you will need to log in every run)")
    p.add_argument("--paper", choices=("book", "auto", "a4"),
                   help="book = text column + gutter (default); "
                        "auto = measured reader frame; a4 = plain A4")
    p.add_argument("--book-page", dest="book_page", metavar="WxH",
                   help="page size for --paper book, e.g. 752x963 "
                        "(margins keep their configured values)")
    p.add_argument("--paper-ratio", dest="paper_ratio", choices=("cover", "fixed"),
                   help="cover = take the sheet height from the cover scan so the "
                        "paper has the book's shape (default); fixed = use the "
                        "height in --book-page as given")
    p.add_argument("--no-cover-bleed", action="store_true", dest="no_cover_bleed",
                   help="leave the cover inside the page margin instead of "
                        "painting it over the whole first page")
    p.add_argument("--no-from-start", action="store_true", dest="no_from_start",
                   help="capture from the page currently open instead of jumping "
                        "back to the cover first")
    p.add_argument("--refresh", action="store_true",
                   help="re-walk the book and overwrite pages/ already captured "
                        "(use after an extraction upgrade)")
    p.add_argument("--no-page-numbers", action="store_true",
                   dest="no_page_numbers",
                   help="do not stamp textbook page numbers / write page labels")
    p.add_argument("--until", dest="until", metavar="TEXT",
                   help="stop before the first page whose title or reader URL "
                        "contains TEXT, e.g. --until 'Chapter 1'")
    args, _ = p.parse_known_args()

    for key in ("output_pdf", "output_html", "max_pages", "pages_dir", "title",
                "paper", "paper_ratio", "user_data_dir", "browser_port",
                "output_dir"):
        val = getattr(args, key, None)
        if val is not None and val != "":
            cfg[key] = val

    if args.output_dir and not args.output_pdf:
        cfg["output_pdf"] = os.path.join(args.output_dir, "textbook.pdf")
    if args.book_page:
        try:
            w, _, h = args.book_page.lower().partition("x")
            old = list(cfg["book_page"])
            cfg["book_page"] = [int(w), int(h)] + old[2:]
        except ValueError:
            raise SystemExit(f"--book-page wants WxH, got {args.book_page!r}")

    if args.attach_port:
        cfg["attach_port"] = args.attach_port
    if args.no_profile:
        cfg["user_data_dir"] = None

    # store_true flags must only ever turn a setting ON, never override
    # config.json back to False just because the flag was omitted.
    for key in ("headless", "fast", "auto_start", "rebuild", "debug",
                "close_browser", "open_pdf", "split_print"):
        if getattr(args, key, False):
            cfg[key] = True

    if args.no_cover_bleed:                 # a negation, so it turns one OFF
        cfg["cover_bleed"] = False

    if cfg.get("fast"):
        cfg["toc"] = False          # legacy meaning: one quick pass, no outline

    if cfg.get("debug"):
        cfg["debug_dir"] = cfg.get("debug_dir") or "debug"

    if args.fresh:
        cfg["resume"] = False
    if args.no_resume:
        cfg["resume"] = False
    if args.no_toc:
        cfg["toc"] = False
    if args.no_inline_assets:
        cfg["inline_assets"] = False
    if args.toc_per_page:
        cfg["toc_per_page"] = True
    if args.no_from_start:
        cfg["from_start"] = False
    if args.refresh:
        cfg["refresh"] = True
    if args.no_page_numbers:
        cfg["page_numbers"] = False
    if args.until:
        cfg["until"] = args.until.strip()

    # Remembered reader URL -> a bare ENTER reopens the book next time.
    last = os.path.join(os.path.dirname(CONFIG_PATH), ".last_url")
    if os.path.exists(last):
        try:
            with open(last, encoding="utf-8") as f:
                cfg["last_url"] = f.read().strip()
        except Exception:
            pass

    return cfg
