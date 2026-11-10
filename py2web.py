"""py2web - inline external resources of an HTML string into a single self-contained
document. Modernized rewrite of the original webpage2html copy that shipped broken
(it lacked the ``save_html`` the ripper called). Used for the optional offline,
self-contained HTML export. Keeps a CLI ``generate(url)`` for parity.
"""
from __future__ import annotations

import base64
import re
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Firefox/65.0"}

_MIME = {
    ".png": "image/png", ".gif": "image/gif", ".ico": "image/x-icon",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".svg": "image/svg+xml",
    ".ttf": "application/x-font-ttf", ".otf": "application/x-font-opentype",
    ".woff": "application/font-woff", ".woff2": "application/font-woff2",
    ".eot": "application/vnd.ms-fontobject", ".css": "text/css",
    ".js": "application/javascript",
}


def _mime_for(url: str, content_type: str | None) -> str:
    if content_type:
        return content_type.split(";")[0].strip()
    ext = urlparse(url).path.lower()
    for k, v in _MIME.items():
        if ext.endswith(k):
            return v
    return "application/octet-stream"


def _fetch(url: str, base_url: str | None):
    target = urljoin(base_url, url) if base_url else url
    try:
        r = requests.get(target, headers=UA, timeout=20)
        if r.ok:
            return r.content, r.headers.get("content-type")
    except Exception:
        return None, None
    return None, None


def _data_uri(data: bytes, mime: str) -> str:
    return f"data:{mime};base64," + base64.b64encode(data).decode()


def _inline_css(css: str, base_url: str | None) -> str:
    def repl(m):
        url = m.group(1).strip(" '\"")
        if url.startswith("data:"):
            return m.group(0)
        data, ct = _fetch(url, base_url)
        if data:
            return f"url({_data_uri(data, _mime_for(url, ct))})"
        return m.group(0)
    return re.sub(r"url\(\s*([^)]*?)\s*\)", repl, css)


def inline_html(html: str, base_url: str | None = None) -> str:
    """Inline <img>, <link rel=stylesheet> and css url(...) into data URIs."""
    soup = BeautifulSoup(html, "lxml")
    for img in soup.find_all("img", src=True):
        if img["src"].startswith("data:"):
            continue
        data, ct = _fetch(img["src"], base_url)
        if data:
            img["src"] = _data_uri(data, _mime_for(img["src"], ct))
    for link in soup.find_all("link", rel="stylesheet", href=True):
        data, ct = _fetch(link["href"], base_url)
        if data:
            style = soup.new_tag("style")
            style.string = _inline_css(data.decode("utf-8", "ignore"), base_url)
            link.replace_with(style)
    for tag in soup.find_all(True):
        if tag.has_attr("style"):
            tag["style"] = _inline_css(tag["style"], base_url)
    return str(soup)


def save_html(html: str, path: str) -> None:
    """Persist an HTML string to disk (UTF-8)."""
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)


def generate(url: str, keep_script: bool = False, verify: bool = True) -> str:
    """Fetch a URL and return a self-contained HTML string (CLI/parity use)."""
    r = requests.get(url, headers=UA, verify=verify)
    return inline_html(r.text, url)


if __name__ == "__main__":
    import sys
    out = generate(sys.argv[1]) if len(sys.argv) > 1 else ""
    print(out)
