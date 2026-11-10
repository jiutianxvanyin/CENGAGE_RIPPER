# CENGAGE_RIPPER

Turn a Cengage eTextbook you already have access to into a **searchable, bookmarked PDF** where
**textbook page numbers are the PDF page numbers**, and in-book cross references become internal
jumps instead of links to a login page.

Everything is extracted as text from the reader's own XHTML — no screenshots, no OCR, no watermark
removal. The result is a normal PDF you can read offline, search, annotate and print.

> **Scope.** This is a personal accessibility/offline-reading tool for material you have legally
> purchased or been given access to. It does not bypass a paywall, does not strip DRM or watermarking,
> and is not built for redistribution. What you do with the output is governed by your licence
> agreement with the publisher. The repository contains **no textbook content**; the two test
> fixtures are synthetic pages written for this project.

---

## Contents

- [What you get](#what-you-get)
- [Requirements](#requirements)
- [Install](#install)
- [Quick start](#quick-start)
- [Command line reference](#command-line-reference)
- [What ends up on disk](#what-ends-up-on-disk)
- [How it works](#how-it-works)
- [Developer notes](#developer-notes)
- [Tests](#tests)
- [Troubleshooting](#troubleshooting)
- [Known limitations](#known-limitations)
- [Repository layout](#repository-layout)
- [Licence](#licence)

---

## What you get

| Feature | Detail |
|---|---|
| Real text | Selectable, searchable, copyable — the reader renders semantic XHTML, and that is what gets printed |
| Figures | Each page's images are fetched **inside** the logged-in session and inlined as `data:` URIs, so charts survive the trip out of the browser |
| One textbook page = one sheet | Page breaks come from the reader's own `epub:type="pagebreak"` markers, not from guessing |
| Printed page numbers | The textbook's own page number is stamped in the footer and written into `/PageLabels`, so `Go to page 214` works in any viewer |
| Bookmarks | A real outline built from page titles and heading levels, with the correct nesting |
| Internal links | "See Table 1.1" jumps inside the PDF; dead web links are unlinked instead of opening a browser |
| Cover | Page size follows the cover scan's aspect ratio; the cover image is painted full-bleed over sheet 1 |
| Uniform typography | One type size for the whole book; over-long pages are shrunk and, if they still do not fit, marked `(cont.)` |
| Resumable | Capture is checkpointed page by page; rerunning continues where it stopped, and `--rebuild` reprints without a browser |

---

## Requirements

- **Python 3.10+**
- **Google Chrome** (any recent stable). No chromedriver, no Selenium.
- Windows, macOS or Linux. The bundled `start_chrome.bat` is Windows-only; everything else is portable.
- A Cengage account with access to the book you want to read, and a browser session you can log into
  by hand once.

---

## Install

```bash
git clone <this-repo> cengage-ripper
cd cengage-ripper
python -m pip install -r requirements.txt
```

For the test suite:

```bash
python -m pip install -r requirements-dev.txt
```

---

## Quick start

```bash
python cengage_ripper.py --max-pages 10
```

1. The tool launches Chrome with its own persistent profile (`chrome-profile/`) and a DevTools port.
2. **Log in by hand** and open the book in the reader.
3. Come back to the terminal and press **ENTER** (if the reader is already the active window you do
   not need to paste a URL).
4. It walks the book, captures each page, prints the PDF, writes the bookmarks and reports the
   absolute output path.

Run it again later:

```bash
python cengage_ripper.py
```

It reattaches to the same Chrome in about a second — **you do not log in again**. The browser is
deliberately left running (`--close-browser` if you want it gone).

Capture the whole book:

```bash
python cengage_ripper.py --fresh
```

Re-print from pages already on disk, without a browser (after changing layout options):

```bash
python cengage_ripper.py --rebuild
```

---

## Command line reference

### Capture

| Flag | Effect |
|---|---|
| `--max-pages N` | Stop after N pages (use it for a trial run) |
| `--auto` | Do not wait for ENTER; poll until the reader appears |
| `--fresh` | Start over; the previous capture is moved to `pages.bak-<timestamp>/`, never deleted |
| `--pages-dir DIR` | Where captured pages live (default `./pages`) |
| `--refresh` | Re-walk the book and overwrite pages already captured (use after upgrading the extractor) |
| `--no-resume` | Disable checkpoint resume |
| `--no-from-start` | Capture from the page currently open instead of walking back to the cover first |
| `--until TEXT` | Stop **before** the first page whose title or reader URL contains `TEXT` |
| `--attach [PORT]` | Only attach to a Chrome already listening on `PORT` (default 9222); never launches one |
| `--port PORT` | DevTools port to launch or reuse (default 9222) |
| `--profile DIR` | Persistent Chrome profile directory (default `./chrome-profile`) |
| `--no-profile` | Throwaway profile — you will have to log in on every run |
| `--close-browser` | Close the capture browser when the run ends |
| `--headless` | Run Chrome headless (log in first, in a windowed run) |
| `--debug` | Dump every page's raw HTML and a DOM report |

### Output and layout

| Flag | Effect |
|---|---|
| `-o PATH` / `--output PATH` | Full output PDF path |
| `--output-dir DIR` | Folder for the PDF/HTML (default `./output`) |
| `--open` | Open the finished PDF when done |
| `--html PATH` | Also export a single self-contained HTML book |
| `--paper book` | **Default.** Sheet = text column + gutter; height follows the cover |
| `--book-page WxH` | Sheet size for `--paper book`, e.g. `752x963` |
| `--paper-ratio cover\|fixed` | `cover` takes the sheet height from the cover scan; `fixed` uses `--book-page` |
| `--no-cover-bleed` | Keep the cover inside the margin instead of painting it over the whole first sheet |
| `--paper a4` | Force A4 portrait |
| `--paper auto` | Use the reader's own measured frame size |
| `--no-page-numbers` | Do not stamp textbook page numbers and do not write `/PageLabels` |
| `--split-print` | Print each reader page on its own sheet: exact bookmark pages, but short sections leave a white tail |
| `--no-toc` / `--toc-per-page` | No bookmarks / one bookmark per page |
| `--no-inline-assets` | Do not embed images (faster, but figures may be missing) |
| `--fast` | Skip bookmark work (the print is already a single pass) |
| `--title TEXT` | PDF title metadata |

### Rebuilding only

`--rebuild` reuses `pages/` and `pages/book.html`, so no login and no network are involved. It is the
fast way to iterate on layout:

```bash
python cengage_ripper.py --rebuild --open
```

`--rebuild` cannot invent pages that were never captured. If front matter is missing, use `--refresh`.

---

## What ends up on disk

```
output/textbook.pdf        the deliverable: searchable, bookmarked, one textbook page per sheet
output/book.html           the merged print source (kept for --rebuild)
pages/                     one cleaned HTML file per reader section + index.json + toc.json
pages.bak-<timestamp>/     the previous capture, kept when you run --fresh
chrome-profile/            the logged-in Chrome profile — treat it as a credential
```

`pages/` and `chrome-profile/` may contain licensed content and a live session. Both are gitignored;
keep them out of backups you share.

---

## How it works

```
Chrome (logged in)
   │  DevTools protocol: one command per step, no chromedriver
   ▼
#iframe-page  ──►  <body class="webreader">   ← the actual book page (semantic XHTML)
   │
   ├─ clean: drop reader chrome, scripts, screen-reader duplicates
   ├─ inline images with fetch(..., {credentials:'include'})  → data: URIs
   ├─ keep every epub:type="pagebreak" marker as span.ripper-pb[data-pb]
   ▼
pages/page_0001.html … + index.json (title, heading level, page label, URL signature)
   ▼
pdf_builder:  refragment paragraphs → cut the flow at pagebreak markers →
              group each textbook page into div.ripper-page[data-pb] →
              measure every group in headless Chrome → pick one zoom for the book →
              print once → merge → stamp footers → write /PageLabels
   ▼
toc + pdfmap: locate each section's first sheet by invisible anchors,
              refine with the text layer, write /Outlines with pikepdf
   ▼
output/textbook.pdf
```

The single most important design decision: **one element represents one textbook page**
(`div.ripper-page[data-pb]`). Once that holds, page numbers, zooming, measuring and bookmarking all
become local operations instead of global guesses.

---

## Developer notes

The rest of this file is about *why* the code looks the way it does. It is written for whoever has to
change it next — including future me.

### Why CDP instead of Selenium

The first version used Selenium. It failed before it could send a single command: the installed
chromedriver and Chrome reported compatible versions, yet every call came back
`WebDriverException: unhandled request` — `window_handles`, `get`, `title`, all of them. Nothing about
selectors or login; the transport itself was broken on that machine.

The DevTools protocol, on the other hand, was completely healthy: navigation, frame trees,
cross-origin frame reads and screenshots all worked. So `cdp.py` talks to Chrome directly over a
WebSocket, and the project has no chromedriver dependency at all.

Two side benefits matter more than the workaround:

- **The real frame tree** (`Page.getFrameTree`) covers cross-origin iframes and shadow-DOM hosted
  frames. There is no "switch into an element and hope", and no `no such window`.
- **We own the browser process**, so the script does not close it. You log in once; later runs
  reattach. (`--close-browser` restores the usual behaviour.)

`cdp.py` also replays commands after a `Session … not found`: Chrome occasionally swaps the target
under you, and a capture run should not die because of it.

### Finding the content frame

`reader_extract.locate_content_frame()` asks for the frame tree, ranks candidates (reader-looking URL
first, sub-frames before the main document, deeper first) and **probes** each one:

```js
{ url, title, bodyClass, dataCgi, epubType, textLength, scrollWidth, scrollHeight }
```

A candidate counts as content only if it has `body.webreader`, a `data-cgi` attribute, or more than
400 characters of text. So a wrong guess cannot fabricate a page — it just fails to match.

In saved snapshots the content lives in `<iframe id="iframe-page" srcdoc="…">`;
`extract_page_html_from_source()` unescapes the `srcdoc` and falls back to an inlined `webreader` body.
That is what makes the fixtures testable without an account.

**Do not use `data-cgi` as a page identity.** In real books it is one value for the whole title — the
cover, title page and copyright page are identical. `signature()` therefore prefers the frame's
document URL, falls back to `data-cgi` plus title plus page number, and only then to the page label.

### Page-break markers are hints; page labels are facts

The reader injects

```html
<span class="pagenumber rs_skip" epub:type="pagebreak" data-after="58" id="page-58"></span>
```

exactly where textbook page 58 begins. Those spans are the only thing that says *where a page starts*,
because the reader re-flows text and never exposes fixed page boxes. Cleanup therefore must not strip
them (they carry `rs_skip`, which used to be discarded as noise); they are renamed to
`span.ripper-pb[data-pb]` and keep their original `id`, because in-book links target it.

Markers are **not** a reliable source of the printed number, though. Front matter regularly has more
markers than pages — a short "About the Authors" section can carry two dozen roman-numeral markers
because the reader parks them all in one `div.block-of-pagenumbers`. The printed number comes from
the reader's own page box (`#pageNumber`), stored per section as `page_label` in `index.json`.

The division of labour:

| Source | Used for |
|---|---|
| `pagebreak` marker | Where to cut the flow |
| `page_label` | What number that page actually carries |
| Marker order | Continuity, when labels are missing |

### Binding textbook pages to PDF pages

The PDF and the book used to have unrelated numbering: PDF 1…N versus textbook 1…600 with roman front
matter in between, so "go to page 58" meant guessing. Now they are the same number, established in six
steps:

1. Capture records `page_label` per section and keeps every pagebreak marker.
2. `pdf_builder.paginate()` turns markers into **block-level** `div.ripper-pb`; when a marker sits
   inside a paragraph it splits the inline ancestors on the way up.
3. `group_pages()` moves everything between two markers into a single
   `div.ripper-page[data-pb]` — one element, one textbook page.
4. Every group gets an invisible anchor at its start:
   `<span class="ripper-anchor">G0042</span>` (1px, transparent, `!important` so the reader's own
   `.book-page span` rules cannot make it visible).
5. After printing, `pdfmap.locate_anchors()` reads the PDF text layer and maps `G0042 → sheet 27`.
   Anchor-based mapping replaced text search here: searching for a page's opening words failed on
   about two thirds of the book.
6. `pagenum.plan()` walks the anchors in order, adds one per sheet between anchors, caps at the next
   anchor when a page needs several sheets, and never invents page 0 before the first anchor.

Then `pagenum.stamp_numbers()` burns the label into the footer and `write_page_labels()` writes
`/PageLabels` segments (`/r` for roman, `/D` for arabic), so a viewer's page box agrees with the
printed page.

Three rules are worth stating explicitly:

- **Sheets before the first anchor** are numbered backwards, stopping at 1.
- **A page taller than one sheet** repeats its number on the extra sheets (`58 (cont.)`) instead of
  running ahead into the next page.
- **Sheets the book never numbered** (title page, copyright page) stay blank. Printing `ii` there
  would be an invention.

### One textbook page per sheet

The reader's "page" is a re-flowing stream: some sections are two paragraphs, others are 4000px tall.
Printing each section separately produces a stack of half-empty sheets, so the default is one
continuous print run — Chrome paginates, and the only forced breaks are the pagebreak markers.

Getting from "continuous run" to "exactly one textbook page per sheet" needed several fixes, each with
a regression test:

| Symptom | Cause | Fix |
|---|---|---|
| Cover sliced across three sheets | The cover section also carries `h1.hideme` ("Book cover") and `p.mediacredit`, which overflow below the image | `_wrap_soup()` detects a cover and keeps only the image |
| Only 20 lines per page | Reader screen CSS beat print CSS: line-height 2.0, 28px side padding | `print_css()` overrides with `!important`: line-height 1.42, zero padding |
| A whole page reduced to "SHOW ANSWER" | Answers live in collapsed `<details>`; only the summary prints. One page of pure answers printed blank and was dropped as empty | `reveal_details()` adds `open` before pagination, and the print CSS repeats it |
| A chapter started one sheet early | The inserted section began with a chapter title while its marker sat after the introduction | `group_pages()` marks that section `carry` so its content joins the marker's page |
| A page number went 32 → 34 | An inserted section with `break-before: auto` shared the tail of sheet 33, which was then skipped as an "insert" sheet | `.ripper-page.ripper-insert { break-before: page }` |

### Uniform type size, and the `zoom` / `max-width` trap

Rather than shrinking each page until it fits — which produces a book where the type size changes
every few pages — the tool measures once and picks **one** zoom for the whole book:

1. Launch headless Chrome, print CSS applied, body pinned to the text column width.
2. Read `getBoundingClientRect().height` for every `div.ripper-page` at 100%.
3. CSS `zoom` re-lays-out as well as scales: at zoom *z* the layout width grows by 1/*z* and the
   line count drops, so height falls roughly as *z*². The zoom that would just fit a page is
   `sqrt(available_height / measured_height)`.
4. Take the 15th percentile of those values (floored at 0.72) as the book-wide zoom. Pages that still
   do not fit are marked `N (cont.)` and continue on the next sheet.

**The trap:** `zoom: 0.8` on a block already lays the element out 1/*z* wider, then scales it back, so
it should still span the full column. But the reader's own `max-width` clamps the widened box back to
its nominal width; the zoom then paints a 20% narrower column, centred, with smaller type. The result
looks like "huge margins and tiny text". `zoom_style()` therefore emits
`zoom:z; max-width:none`. Do **not** add `width: 1/z%` as a "fix": Chrome multiplies percentage
widths on zoomed elements a second time and the box overflows the sheet.

Measured effect: the text column went from 77% to about 95% of the sheet width, and the book got
shorter. The check is not visual — it is the x-extent of `get_text("words")` in the finished PDF.

Because the paper height is chosen from the cover aspect ratio, a book whose cover is unusually tall
gets a taller sheet; the measurement pass re-runs after that height is known.

### Answers become an `Answer:` line

Answers are in `<details class="toggleAnswer"><summary>SHOW ANSWER</summary>`. On paper there is
nothing to click, and the summary wastes a line while the answer starts its own paragraph.

`format_answers()` replaces the whole `details` element with `div.ripper-answer`:

- a bold `Answer: ` prefix followed by the text, all `display:inline`, so the answer does not break
  across lines;
- indented `1.7em` under the question;
- the reader's green check mark (`.correct::before`) removed with `content:none`;
- answers that contain a table or a list are **not** flattened — there, structure *is* the answer. They
  get `ripper-answer-block`, keep the prefix on its own line and keep the table intact;
- `break-inside: avoid` keeps an answer off the bottom edge of a sheet.

### Bookmarks: anchors first, text search to refine

Text search alone is not good enough for bookmark pages: a heading like `Content` matches body prose
and one wrong match shifts every later bookmark.

So each `.book-page` carries `data-src="N"` (the Nth captured section) and each group knows which
sheet it starts on. The bookmark for a section is the sheet of its group. `pdfmap.refine_sections()`
then does a *bounded* text search — only between the previous section's sheet and the next one's, and
only for a heading that occupies a line by itself — to correct small offsets.

Sections whose `page_label` is known are pinned to that sheet outright: the sheet printed as page 22
*is* page 22.

When a heading cannot be located (an image title, or a title broken across lines in the text layer),
the bookmark stays on the previous section and a warning is printed. That is a known, reported
condition, not silent corruption.

### Links: from "open a web page" to "jump inside the PDF"

Captured links point at `https://ebooks.cengage.com/...`, so clicking one leaves the PDF, asks for a
network connection and possibly a login. The semantics are internal references ("see Table 1.1"), and
the reader encodes them as `#cgi` fragments plus `data-page-id`.

`rewrite_links()` therefore:

| Original | Becomes |
|---|---|
| `#frag` and `frag` exists in this book | unchanged — an internal GoTo |
| `data-page-id="content-xyz"` or a URL containing `/content-xyz` | `href="#sec-content-xyz"` plus an empty anchor before that section |
| Any other `cengage.com` URL (external resources, login) | `href` removed, text kept, class `ripper-deadlink` |
| Any non-Cengage URL | unchanged |

This relies on Chrome writing referenced internal anchors into `/Dests`; PyMuPDF confirms the result is
a `kind=4` GoTo. `target="_parent"` is stripped — it means nothing in a PDF.

Each print reports one line:

```
links: 13 in-book anchor(s) -> internal jumps, 4 web-only link(s) unlinked
```

If that line is missing, the pages were captured by an older version; `--rebuild` will not fix it,
`--refresh` will.

### Paragraphs that arrive as blocks

The publisher's conversion tool turns inline elements into blocks:

```html
<p>…the scale of measurement is considered a </p>
<div class="keytermentry"><button class="keyterm">nominal scale</button></div>
. For example, referring to the data in Table 1.1 …
```

Closing the `<p>` ends the sentence; the loose text `. For example …` then lays out as its own block at
container width, which is why the right edge is ragged and line spacing appears to jump. Footnote
markers are worse: a `div.footnote-wrapper` containing only `*` puts a blank line with a star in the
middle of a paragraph.

`pdf_builder.refragment()` puts the paragraph back together before printing:

| Action | Rule | Result |
|---|---|---|
| Inline | `div/button.keytermentry`, `.keyterm`, `.footnote-wrapper` → `span` | terms and footnote stars rejoin the sentence |
| Adopt loose text | bare text in a container is wrapped in a new `<p>` | line width returns to the text column |
| Merge continuations | previous block lacks terminal punctuation, **or** the next block starts lower-case / with punctuation | one paragraph again |
| Do not merge | previous block ends with `[.!?:;"')\]}）”’` and the next starts upper-case | a genuine new paragraph |
| Barrier | `<h1..h6>`, `<table>`, `<figure>`, pagebreak markers | headings, tables and page edges are never swallowed |

`div.mathexpr` (display math) is deliberately **not** inlined: it is meant to occupy its own line.

Spacing is fixed too: `.book-page p { margin: 0 0 .62em; orphans: 2; widows: 2 }` with
`p:first-child { margin-top: 0 }`.

### Paper shape and cover bleed

A hard-coded `752x1500` sheet is a 1:2 strip — one page of text spread over two sheets' worth of
height, loose lines, small figures. The book's real shape does not need to be guessed: **the cover
scan is exactly one page**.

```html
<img class="coverimage" epub:type="cover" role="doc-cover" src="data:image/jpeg;base64,…">
```

`cover_aspect()` reads its height/width ratio and `book_spec()` derives the sheet height from it, so
text pages inherit the printed book's proportions.

Full-bleed is a separate problem: CSS cannot paint into the `@page` margin. Two attempts failed —
`@page cover { margin: 0 }` with `page: cover` is ignored by Chrome's `--print-to-pdf`, and a negative
margin that makes the image one page tall pushes it onto sheet 2 and leaves sheet 1 blank. The fix
happens at the PDF layer:

```python
page.insert_image(page.rect, stream=cover_bytes, overlay=True)
```

One full-page rectangle, and the image's bbox lands on the whole sheet. With `--paper-ratio fixed` the
image keeps its aspect ratio instead of stretching (a thin margin beats a squashed cover).
`--no-cover-bleed` disables the step. The cover sheet gets no page number.

### Front matter is not like the body

Walking from the cover to Chapter 1 crosses Cover, Title Page, Copyright, About the Authors, Preface
and Acknowledgments, and four things behave differently:

1. **Markers lie, the page box does not.** A short front-matter section can hold two dozen roman
   markers for three pages of content; they are parked together in `div.block-of-pagenumbers`. Use
   `page_label` for numbering, markers only for cutting.
2. **Parked markers must not each force a sheet.** A container of empty anchors has nothing to push,
   so `break-before` does nothing there; treating each marker as a page prints a stack of blanks. The
   rule is: a group of parked markers contributes **one** break after the container, and the rest are
   recorded as phantom/skip, neither printed nor counted as missing.
   > The bug this hid: when asking "is there content after the last marker", `node.next_elements`
   > walks the container's own subtree first, finds the second marker and answers "nothing after".
   > Real breaks then stayed inside a zero-height container and two pages were glued together. Ask
   > `marks[-1]`, not the container.
3. **Front matter breaks per section.** It has no pagebreak markers, so section boundaries are page
   boundaries (cover, title page, copyright page each get a sheet). The body still flows.
4. **The cover bookmark points at sheet 1.** The cover title is an image, so text matching would land
   on the title page instead.

### Walking back to the cover

Capture starts wherever the reader is. If you left it in the middle of chapter 6, that is where the
book starts — not a bug, just a missing step. With `from_start` on (the default), the tool tries three
routes before capturing:

1. **Rewrite the URL**: replace `/content-<anything>` with `/content-cover` and navigate.
2. **Click the contents panel**: a set of selectors around `[data-id="content-cover"]`.
3. **Walk back**: press `#previous-page-button` until it is disabled (capped at 600 presses).

The log says which one worked: `going back to the cover (reader URL)` /
`jumped to the cover through the contents panel` / `walking back to the first page`.

`--no-from-start` skips all of this. Note that front matter missing from an old capture needs
`--refresh`; `--rebuild` only reprints what is already on disk.

`--until` stops *before* a match. It tests three things — the document title, the frame URL, and the
`ch_01` style URL fragment — because the reader's `<title>` usually drops the chapter number
(`Chapter 1. Data and Statistics` becomes `Data and Statistics`), so comparing titles alone never
matches.

### Page-break detection during capture

| Question | Signal |
|---|---|
| Did the page change? | The frame's document URL changed (each page has its own URL); fall back to `data-cgi`, then title + page number |
| Is this the last page? | `#next-page-button` is `disabled` / `aria-disabled` / carries a disabled class, or the control disappears |
| Is it stuck? | The page identity is unchanged after `advance_timeout` seconds — stop instead of recapturing |
| The button will not click? | JS click → real mouse click → key press → synthetic key event inside the frame |

---

## Tests

```bash
python -m pytest tests/ -q
python selftest_offline.py
python selftest_browser.py
python selftest_browser.py --new-window
```

**`pytest`** — 133 tests, no browser, no account. They cover extraction from a saved snapshot, cleanup,
outline building and nesting, pagebreak handling, pagination mapping, paragraph refragmentation, zoom
width compensation, anchor placement, cover aspect/bleed, and the capture loop against a fake browser.

**`selftest_offline.py`** — clones a fixture into four synthetic sections (with markers planted at
pages 11–14), runs the real `assemble()` path through Chrome, and asserts: 4 sections → 4 sheets,
bookmarks on their own pages, footers `11/12/13/14`, and a real text layer.

**`selftest_browser.py`** — launches a real Chrome against a fake reader (`#pageNumber`,
`#next-page-button`, `#iframe-page`, three pages labelled 41/42/43), runs the production
`capture()` → `assemble()` path, and asserts 3 pages captured, a 4-page PDF, bookmarks, real text,
footers `41/42/43`, and the binding `p.41 = PDF p.1`. `--new-window` makes the reader open via
`window.open` and asserts the capture follows it.

The fixtures (`fixtures_sample.html`, `fixtures_shell_sample.html`) are generated by
`tools/make_fixtures.py`. They are synthetic: same DOM shape as a saved reader page, no publisher
content. `fixtures_shell_sample.html` is a shell with no inline frame content — extraction is expected
to raise on it.

Two auditing scripts help when a real capture looks wrong:

```bash
python verify_capture.py --pages-dir pages --limit 40   # live page vs saved page, side by side
python check_final.py output/textbook.pdf               # sheets, bookmarks, footer numbers, gaps
```

`verify_capture.py` reopens each reader page in the browser and compares the live document's text
length with the file on disk, so a page that was saved truncated shows up as a ratio far below 1.0.
`check_final.py` reads the finished PDF and reports numbering continuity, `(cont.)` counts, missing
labels and bookmarks whose target sheet does not contain their heading.

---

## Troubleshooting

```bash
python diagnose.py
```

It prints every frame's depth, URL, `body` class, `data-cgi`, text length and the available page
controls, and writes `diagnose/{summary.json, page_source.html, screen.png}`.

Common cases:

| Symptom | Likely cause | What to do |
|---|---|---|
| `Session with given id not found` | Chrome replaced the target tab | Should self-heal by reattaching; if not, rerun with `--attach` |
| `No webreader content found` | Wrong window, or you are not on a book page | Run `diagnose.py`, check `body.webreader` / `data-cgi` |
| `page never changed within 15s` | The page identity is wrong, or the click did not land | Check that `signature()` is using the frame URL; try `--debug` |
| Captured pages look truncated | An old extractor version | `--refresh`, then `--rebuild` |
| Figures missing | Image fetch failed (session or CORS) | The log prints `WARN: n/m image(s) could not be inlined`; retry without `--no-inline-assets` |
| Bookmarks all point at one page | Old capture without `data-src` | `--refresh` |
| Page numbers restart or skip | Parked markers in front matter | Expected; see [Front matter](#front-matter-is-not-like-the-body) |

When the reader's front end changes, these are the places to edit:

- `reader_extract.FRAME_URL_HINTS` — how a frame is recognised as the content stream.
- `cengage_ripper.SHELL_STATE_JS` — `#pageNumber`, `#next-page-button`, navigation selectors.
- `goto_start()` in `cengage_ripper.py` — the `/content-cover` rewrite and the contents-panel
  selectors used to walk back to page one.

---

## Known limitations

These are honest limits, not a to-do list that will disappear:

- **Long pages spill.** A textbook page that cannot fit on one sheet at the book-wide zoom continues
  on the next sheet and is labelled `N (cont.)`. Printing it at 100% would break the uniform type
  size, which was judged worse.
- **Some pages have no printed number.** Markers the reader parks in a group (typically front matter
  and full-page tables) can leave a page's content in the PDF without its own stamp. The content is
  there; the number is not invented.
- **Full-page tables that do not exist in the HTML.** Some tables are drawn by the reader's
  JavaScript and have no row/column markup to print. Their page may span two or three sheets with the
  number repeated.
- **A few bookmarks stay on the previous section** when the heading is an image or is split across
  lines in the text layer. Reported as a warning.
- **Cengage-specific.** The frame heuristics, page controls and link rewriting assume the Cengage
  webreader. Other EPUB readers would need their own `FRAME_URL_HINTS` and page controls.
- **Not a downloader.** The tool walks the reader page by page at human speed. It is not built to
  mirror a library.

---

## Repository layout

```
cengage_ripper.py     orchestration: attach/launch Chrome, capture loop, assembly, report
cdp.py                DevTools transport: process launch, attach, frame tree, eval, click, screenshot
reader_extract.py     locate the webreader document in the frame tree; clean it; keep pagebreak markers
toc.py                page metadata -> outline entries -> nested /Outlines via pikepdf
pdfmap.py             locate each section's first sheet in the finished PDF (anchors + refined text search)
pagenum.py            textbook page <-> PDF sheet: plan(), stamp_numbers(), write_page_labels()
pdf_builder.py        print CSS, refragment, paginate, group pages, measure/zoom, cover bleed, links
py2web.py             optional self-contained HTML export
config.py             defaults -> config.json -> command line
diagnose.py           frame inventory and a DOM snapshot for bug reports
selftest_offline.py   fixture -> real print -> assertions (no account)
selftest_browser.py   fake reader in a real Chrome -> assertions (no account)
verify_capture.py     audit a capture: live page vs saved page, page by page
check_final.py        audit a finished PDF: sheets, bookmarks, footer numbering, gaps
tools/make_fixtures.py regenerates the synthetic fixtures
tests/                pytest suite
```

---

## Licence

MIT — see [LICENSE](LICENSE). The licence covers this code, not any book you process with it.
