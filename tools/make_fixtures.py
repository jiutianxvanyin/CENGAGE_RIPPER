"""One-off generator for fixtures_sample.html / fixtures_shell_sample.html.

The published repo must not carry a real textbook snapshot, so the test
fixtures are synthesised here with the same DOM shape the extractor expects:
a reader shell with an <iframe id="iframe-page" srcdoc="..."> hosting a
<body class="webreader" data-cgi="..." epub:type="..."> document plus real
pagebreak markers.

Usage:  python tools/make_fixtures.py
"""
from __future__ import annotations

import html
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PAGE = """<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head>
<meta charset="utf-8"/>
<title>{title}</title>
<style>
  html {{ background:#fff; }}
  body.webreader {{ margin:0 auto; max-width:720px; padding:24px 28px;
                    font:16px/2.0 Georgia, serif; color:#1b1b1b; }}
  h1 {{ font-size:1.6em; line-height:1.3; margin:0 0 .8em; }}
</style>
</head>
<body class="webreader" data-cgi="SAMPLEBOOK0001" epub:type="{epub_type}">
<section class="page-section" id="sec-{slug}">
<h1 class="page-title">{title}</h1>
<p class="page-statement">{title}</p>
<p>Sample Textbook: Applied Data Analysis, First Edition</p>
<p>COPYRIGHT &copy; 2024 Example Academic Press. All rights reserved. No part of
this work covered by the copyright herein may be reproduced or distributed in any
form or by any means without the prior written permission of the publisher.</p>
<p>ISBN: 978-0-000000-00-0</p>
<p>Example Academic Press, 100 Sample Street, Springfield</p>
<p>This synthetic page exists only so the extractor, the cleaner, the outline
builder and the print pipeline can be tested without shipping any real
publisher content. The wording is deliberately generic, the identifiers are
fake, and the structure mirrors a reader page closely enough to exercise every
branch: a frame-hosted webreader body, a title, a first heading, a book-level
data-cgi attribute, an epub:type role, and pagebreak markers.</p>
<p>The scale of measurement is considered a</p>
<div class="keytermentry"><button class="keyterm">nominal scale</button></div>
<p>. For example, referring to the data in Table 1.1, the labels are names and
carry no order, so arithmetic on them would be meaningless.</p>
<p>Fitch Rating: the rating is appraised by the Fitch Group<div class="footnote-wrapper">*</div>; the ratings range from a high of AAA to a low of D, and the scale is reviewed every year.</p>
<p>Data sets come in many shapes. A cross-sectional sample observes many units at
one moment, while a time series follows one unit over many periods. Choosing the
wrong shape makes a perfectly clean table answer the wrong question, which is why
the first chapter spends so long on definitions rather than formulas.</p>
<p>Every table should be readable without the surrounding prose. Label the units,
state the sample size, and date the observations. A reader who has to hunt for
the unit of measurement will misread the magnitude by an order of ten.</p>
<span class="pagenumber rs_skip" epub:type="pagebreak" data-after="{pb_first}" id="page-{pb_first}"></span>
<p>Descriptive statistics summarise what is already in the table: a mean, a
median, a spread. Inferential statistics go the other way and ask what the table
implies about the population it was drawn from. The rest of the book alternates
between the two, and the exercises at the end of each chapter usually need
both.</p>
<p>A graph is an argument. A bar chart compares categories, a histogram shows the
shape of a distribution, and a dot plot puts every observation on the line so the
reader can count them. Do not decorate a chart with a third dimension: it adds
ink, not information.</p>
<details class="toggleAnswer"><summary>SHOW ANSWER</summary>
<div class="answer"><p class="correct">Descriptive statistics describe the sample; inferential statistics generalise to the population.</p></div>
</details>
<span class="pagenumber rs_skip" epub:type="pagebreak" data-after="{pb_second}" id="page-{pb_second}"></span>
<p>Probability supplies the bridge between the sample and the population. Once a
sampling distribution is known, an estimate can be given an interval, and the
interval can be read as a statement about repeated samples rather than about one
lucky table.</p>
<p>Regression extends the same logic to two variables. The slope is an estimate,
the standard error says how precise it is, and the residual plot says whether the
straight line was a reasonable summary in the first place.</p>
</section>
</body>
</html>
"""


def build_page(title: str, slug: str, epub_type: str = "frontmatter",
               pb_first: str = "iv", pb_second: str = "v") -> str:
    return PAGE.format(title=title, slug=slug, epub_type=epub_type,
                       pb_first=pb_first, pb_second=pb_second)


SHELL = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<title>eTextbook: Sample Book - Reader</title>
<style>
  body {{ margin:0; font:14px system-ui, sans-serif; }}
  #iframe-page {{ width:100%; height:calc(100vh - 64px); border:0; }}
  .page-controls {{ display:flex; gap:12px; align-items:center; padding:8px 16px; }}
  #onetrust-consent-sdk {{ position:fixed; inset:auto 0 0 0; background:#eee; }}
</style>
</head>
<body class="reader-shell">
<header class="reader-top">
  <button id="toc-toggle">Table of Contents</button>
  <button id="search-toggle">Search</button>
  <span class="screen-reader-only">Selection mode disabled.</span>
</header>
<div id="onetrust-consent-sdk"><p>Privacy Preference Center</p></div>
<iframe id="iframe-page" name="iframe-page" title="page content" srcdoc="{srcdoc}"></iframe>
<div class="block-of-pagenumbers">
  <span class="pagenumber rs_skip" epub:type="pagebreak" data-after="from i to xxi"></span>
  <span class="pagenumber rs_skip" epub:type="pagebreak" data-after="xxii"></span>
</div>
<div class="page-controls">
  <button id="previous-page-button">Previous page</button>
  <span id="pageNumber">iv</span>
  <button id="next-page-button">Next page</button>
</div>
<script>window.__READER_STATE__ = {{page: "iv"}};</script>
</body>
</html>
"""

# A shell saved with "Save as" carries no inline frame content: extraction must
# refuse it rather than invent a page.
BARE_SHELL = SHELL.replace(
    '<iframe id="iframe-page" name="iframe-page" title="page content" srcdoc="{srcdoc}"></iframe>',
    '<iframe id="iframe-page" name="iframe-page" title="page content" src="about:blank"></iframe>')

if __name__ == "__main__":
    page = build_page("Copyright Statement", "copyright")
    (ROOT / "fixtures_sample.html").write_text(
        SHELL.format(srcdoc=html.escape(page, quote=True)), encoding="utf-8")
    (ROOT / "fixtures_shell_sample.html").write_text(BARE_SHELL, encoding="utf-8")
    print("wrote fixtures_sample.html / fixtures_shell_sample.html")
