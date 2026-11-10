"""Final acceptance check for the printed book.

    python check_final.py output/textbook.pdf

Reports sheet count, bookmark count, footer numbering (digits / roman /
"(cont.)"), numbering gaps, blank or near-empty sheets, and whether each
bookmark lands on a sheet that actually contains its heading.
"""
from __future__ import annotations

import collections
import re
import sys

import fitz


def footer_of(page, height: float) -> str:
    """Text in the bottom band of the sheet (page number / continuation mark).

    Only the lowest line counts: a table's last row also sits near the bottom,
    and concatenating every span below ``height - 40`` produced footers such as
    "12121212718".
    """
    rows: dict[int, str] = {}
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                y = span["bbox"][3]
                if y > height - 34:
                    key = int(y // 4)
                    rows[key] = rows.get(key, "") + span["text"]
    if not rows:
        return ""
    return rows[max(rows)].strip()


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else "output/textbook.pdf"
    doc = fitz.open(path)
    height = doc[0].rect.height
    width = doc[0].rect.width
    print(f"sheets   : {doc.page_count}")
    print(f"bookmarks: {len(doc.get_toc())}")
    print(f"page     : {width:.0f}x{height:.0f} pt")

    kinds = collections.Counter()
    numbers: list[tuple[int, int]] = []
    cont = 0
    roman = 0
    other = 0
    for i, page in enumerate(doc, 1):
        foot = footer_of(page, height)
        if re.fullmatch(r"\d{1,4}", foot):
            kinds["digit"] += 1
            numbers.append((i, int(foot)))
        elif re.fullmatch(r"(\d{1,4}) \(cont\.\)", foot):
            kinds["cont"] += 1
            cont += 1
            # A continuation sheet carries the same number as its parent page,
            # so it belongs in the sequence: skipping it made every spill look
            # like a numbering gap.
            numbers.append((i, int(re.match(r"\d+", foot).group())))
        elif re.fullmatch(r"[ivxlc]+", foot):
            kinds["roman"] += 1
            roman += 1
        elif foot:
            kinds["other"] += 1
            other += 1
    print(f"footers  : {kinds.most_common(4)}")

    gaps = []
    for (p1, v1), (p2, v2) in zip(numbers, numbers[1:]):
        if v2 - v1 not in (0, 1):
            gaps.append((p1, v1, p2, v2))
    print(f"numbered : {len(numbers)} sheets, {len(set(v for _, v in numbers))} "
          f"distinct values, max {max((v for _, v in numbers), default=0)}")
    print(f"gaps     : {len(gaps)} {gaps[:6]}")
    print(f"cont     : {cont} sheet(s)")

    chars = [len(p.get_text().strip()) for p in doc]
    blank = [i + 1 for i, c in enumerate(chars) if c < 20]
    thin = [i + 1 for i, c in enumerate(chars) if 20 <= c < 150]
    print(f"blank    : {len(blank)} {blank[:12]}")
    print(f"thin     : {len(thin)} {thin[:12]}")

    toc = doc.get_toc(simple=True)
    misses = []
    for level, title, page_no in toc:
        if not (1 <= page_no <= doc.page_count):
            misses.append((page_no, title, "out of range"))
            continue
        body = " ".join(doc[page_no - 1].get_text().split()).lower()
        key = re.sub(r"[^a-z0-9 ]", " ", title.lower()).split()
        key = " ".join(key[:4])
        if key and key not in re.sub(r"[^a-z0-9 ]", " ", body):
            misses.append((page_no, title, "heading not on this sheet"))
    print(f"bookmark check: {len(toc) - len(misses)}/{len(toc)} matched")
    for m in misses[:10]:
        print(f"   p{m[0]} {m[1][:52]!r} - {m[2]}")


if __name__ == "__main__":
    main()
