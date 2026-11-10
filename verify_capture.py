"""Walk the reader page by page and compare the live page with what was saved.

For every reader page it reads the *live* content document in the browser
(``textContent`` length) and the length of the matching ``page_NNNN.html`` on
disk.  A page whose saved copy is far shorter than the live one was captured
before the reader finished rendering.

    python verify_capture.py --pages-dir pages.full --limit 40
    python verify_capture.py --pages-dir pages.full            # whole book

Nothing is written except ``verify_report.json`` next to the pages folder.
"""
from __future__ import annotations

import argparse
import json
import os
import time

import cdp
import cengage_ripper as R
from bs4 import BeautifulSoup


def saved_len(path: str) -> int:
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            s = BeautifulSoup(f.read(), "lxml")
        for t in s(["script", "style"]):
            t.decompose()
        return len(s.get_text(strip=True))
    except Exception:
        return -1


def live_len(browser, frame_id: str) -> int:
    """Length of the page as the capture step would have saved it.

    Reading ``document.body.textContent`` is useless here: the reader keeps the
    whole book's DOM inside the frame, so every page reports ~100k characters.
    The saved file is a cleaned copy of the frame's HTML, so the fair
    comparison is "clean the live HTML the same way and measure that".
    """
    try:
        raw = browser.html(frame_id)
        cleaned = R.clean_webreader(raw)
        s = BeautifulSoup(cleaned, "lxml")
        for t in s(["script", "style"]):
            t.decompose()
        return len(s.get_text(strip=True))
    except Exception:
        return -1


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages-dir", default="pages.full")
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--wait", type=float, default=None,
                    help="seconds to wait after a page turn (default: config)")
    ap.add_argument("--out", default="verify_report.json")
    args = ap.parse_args()

    cfg = json.load(open("config.json", encoding="utf-8"))
    cfg["attach_port"] = 0
    cfg["browser_port"] = int(cfg.get("browser_port") or 9222)
    cfg.setdefault("user_data_dir", "chrome-profile")
    if args.wait is not None:
        cfg["page_wait"] = args.wait

    with open(os.path.join(args.pages_dir, "index.json"), encoding="utf-8") as f:
        idx = json.load(f)
    if isinstance(idx, dict):
        idx = idx.get("pages") or idx.get("metas") or []

    browser = cdp.launch_or_attach(cfg)
    content = R.ContentFrame()
    R.focus_reader_tab(browser)
    print("  back to the cover ...", flush=True)
    R.goto_start(browser, cfg, content)
    time.sleep(max(cfg["page_wait"], 1.0))

    rows = []
    prev_sig = None
    for i in range(min(args.limit, len(idx))):
        state = R.read_state(browser, cfg, content)
        fid = content.frame_id
        live = live_len(browser, fid) if fid else -1
        meta = idx[i] if i < len(idx) else {}
        path = os.path.join(args.pages_dir, meta.get("file") or "")
        disk = saved_len(path) if os.path.exists(path) else -1
        ratio = (disk / live) if (live > 0 and disk >= 0) else None
        rows.append({
            "n": i + 1,
            "file": meta.get("file"),
            "title": (state.get("title") or meta.get("title") or "")[:60],
            "live": live,
            "disk": disk,
            "ratio": round(ratio, 2) if ratio is not None else None,
            "label": state.get("page_label") or "",
        })
        flag = ""
        if live > 0 and disk >= 0:
            if ratio is not None and ratio < 0.6:
                flag = "  <-- SHORT"
            elif ratio > 1.4:
                flag = "  (disk longer: cleaned vs raw)"
        print(f"  [{i+1:>3}] live {live:>6}  disk {disk:>6}  "
              f"{'' if ratio is None else f'{ratio:>4.2f}'}  "
              f"{rows[-1]['title']!r}{flag}", flush=True)

        if not state.get("next_present") or not state.get("next_enabled"):
            print("  end of book")
            break
        if not R.goto_next_page(browser, cfg, content):
            print("  could not turn the page")
            break
        if cfg["page_wait"]:
            time.sleep(cfg["page_wait"])
        # Wait for the reader to actually swap the document: reading too early
        # would report the previous page twice.
        before = state.get("frame_url") or ""
        deadline = time.time() + float(cfg.get("advance_timeout") or 15)
        while time.time() < deadline:
            st = R.read_state(browser, cfg, content)
            if (st.get("frame_url") or "") != before:
                break
            time.sleep(0.3)

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1)

    short = [r for r in rows if r["ratio"] is not None and r["ratio"] < 0.6]
    print()
    print(f"  pages checked : {len(rows)}")
    print(f"  short (<60%)  : {len(short)}")
    for r in short[:12]:
        print(f"    {r['file']}  live {r['live']}  disk {r['disk']}  {r['title']!r}")
    print(f"  report -> {args.out}")


if __name__ == "__main__":
    main()
