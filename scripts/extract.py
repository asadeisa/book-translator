"""Turn a source book into the common block model.

Supported inputs: PDF (text-based), EPUB, HTML, DOCX, Markdown, plain text.

Block model (source/book.json)::

    {"meta": {"title": ..., "author": ..., "source": ..., "format": ...},
     "chapters": [{"id": "c001", "title": "...", "blocks": [block, ...]}]}

Block types:
    heading  {level, text}           para    {text}
    item     {text, ordered, depth}  quote   {text}
    code     {text}                  (never translated, kept byte-for-byte)
    table    {rows: [[cell, ...]], header: bool}
    figure   {file, alt, labels}     (image copied; text inside is not translated)
    caption  {text}                  rule    {}

Translatable text uses a small inline markup that every renderer understands:
``**bold**``, ``*italic*``, ```code```, ``[text](url)``. Literal ``*``, `````,
``[`` and ``]`` in source prose are backslash-escaped.
"""
from __future__ import annotations

import html
import io
import json
import posixpath
import re
import zipfile
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path

ESC_RE = re.compile(r"([*`\[\]])")
LIST_RE = re.compile(r"^\s*(?:([\u2022\u25CF\u25CB\u25E6\u25AA\u25A0\u2023\u2043\u2013\u2014\-\*\u00B7])|(\d{1,3}[.)]|[a-zA-Z][.)]|[ivxIVX]{1,4}[.)]))\s+(?=\S)")
LIST_MARK_ONLY = re.compile(r"(?:\d{1,3}|[a-zA-Z]|[ivxIVX]{1,4})[.)]|[\u2022\u25CF\u25CB\u25E6\u25AA\u25A0\u2013\-*\u00B7]")
TERMINAL = tuple('.!?:;"\u201d\u00bb)\u2026\u3002\uff01\uff1f\u061f\u06d4\u0964')


def esc(text: str) -> str:
    return ESC_RE.sub(r"\\\1", text)


def clean_ws(text: str) -> str:
    return re.sub(r"[ \t\r\f\v\u00a0]+", " ", text).strip()


def fix_ligatures(text: str) -> str:
    return (text.replace("\ufb00", "ff").replace("\ufb01", "fi").replace("\ufb02", "fl")
            .replace("\ufb03", "ffi").replace("\ufb04", "ffl").replace("\ufb05", "st")
            .replace("\ufb06", "st").replace("\u00ad", ""))


class Book:
    """Accumulates chapters and blocks while an extractor runs."""

    def __init__(self, source: Path, fmt: str, assets: Path):
        self.meta = {"title": "", "author": "", "source": source.name, "format": fmt, "language": ""}
        self.chapters: list[dict] = []
        self.assets = assets
        self.n_fig = 0
        self.notes: list[str] = []      # extraction warnings for the report

    def chapter(self, title: str = "") -> dict:
        ch = {"id": f"c{len(self.chapters) + 1:03d}", "title": clean_ws(title), "blocks": []}
        self.chapters.append(ch)
        return ch

    @property
    def cur(self) -> dict:
        if not self.chapters:
            self.chapter("")
        return self.chapters[-1]

    def add(self, block: dict) -> None:
        t = block.get("type")
        if t in ("para", "item", "quote", "caption", "heading"):
            block["text"] = block["text"].strip()
            if not block["text"]:
                return
        if t == "code" and not block["text"].strip():
            return
        self.cur["blocks"].append(block)

    def save_image(self, data: bytes, ext: str) -> str:
        self.n_fig += 1
        ext = (ext or "png").lower().lstrip(".")
        if ext == "jpeg":
            ext = "jpg"
        name = f"fig_{self.n_fig:04d}.{ext}"
        self.assets.mkdir(parents=True, exist_ok=True)
        (self.assets / name).write_bytes(data)
        return f"assets/{name}"

    def to_json(self) -> dict:
        # Drop empty untitled chapters (e.g. blank front pages).
        chapters = [c for c in self.chapters if c["blocks"] or c["title"]]
        for i, c in enumerate(chapters, 1):
            c["id"] = f"c{i:03d}"
        if not self.meta["title"]:
            self.meta["title"] = Path(self.meta["source"]).stem.replace("_", " ")
        # glyphs without a Unicode mapping come out as private-use characters
        pua = re.compile(r"\S*[-]\S*")
        bad = [(c["id"], w) for c in chapters for b in c["blocks"] if b.get("type") != "code"
               for w in pua.findall(b.get("text", ""))]
        if bad:
            sample = ", ".join(f"{cid}: {w!r}" for cid, w in bad[:6])
            self.notes.append(f"{len(bad)} words contain undecodable glyphs (private-use characters, "
                              f"usually ligatures in a font without a text mapping), e.g. {sample}. "
                              "Translators see them as gaps; check those chapters with `show`.")
        return {"meta": self.meta, "chapters": chapters, "notes": self.notes}


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------

MONO_HINTS = ("mono", "courier", "consol", "menlo", "inconsolata", "code",
              "fixed", "lucidaconsole", "typewriter", "cascadia", "firacode")


def _is_mono(span) -> bool:
    f = span["font"].lower().replace(" ", "")
    return bool(span["flags"] & 8) or any(h in f for h in MONO_HINTS)


def _is_bold(span) -> bool:
    f = span["font"].lower()
    return bool(span["flags"] & 16) or "bold" in f or "black" in f or "heavy" in f


def _is_italic(span) -> bool:
    f = span["font"].lower()
    return bool(span["flags"] & 2) or "italic" in f or "oblique" in f


def _rect_area(r) -> float:
    return max(0.0, r[2] - r[0]) * max(0.0, r[3] - r[1])


def _inter(a, b) -> float:
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[2], b[2]), min(a[3], b[3])
    return _rect_area((x0, y0, x1, y1)) if x1 > x0 and y1 > y0 else 0.0


def _inside(inner, outer, frac=0.6) -> bool:
    a = _rect_area(inner)
    return a > 0 and _inter(inner, outer) / a >= frac


def _merge_rects(rects, pad=3.0):
    rects = [list(r) for r in rects]
    changed = True
    while changed:
        changed = False
        out = []
        for r in rects:
            for o in out:
                if (r[0] - pad <= o[2] and o[0] - pad <= r[2]
                        and r[1] - pad <= o[3] and o[1] - pad <= r[3]):
                    o[0], o[1] = min(o[0], r[0]), min(o[1], r[1])
                    o[2], o[3] = max(o[2], r[2]), max(o[3], r[3])
                    o[4] = o[4] + r[4]
                    changed = True
                    break
            else:
                out.append(r)
        rects = out
    return rects


def _span_markup(spans, links, whole_bold, whole_italic) -> str:
    """Join the spans of one line into marked-up text."""
    parts = []
    for s in spans:
        t = fix_ligatures(s["text"])
        if not t:
            continue
        style = ""
        if s["mono"]:
            style = "code"
        elif s["bold"] and not whole_bold:
            style = "b"
        elif s["italic"] and not whole_italic:
            style = "i"
        url = None
        for lr, uri in links:
            cx, cy = (s["bbox"][0] + s["bbox"][2]) / 2, (s["bbox"][1] + s["bbox"][3]) / 2
            if lr[0] <= cx <= lr[2] and lr[1] <= cy <= lr[3]:
                url = uri
                break
        parts.append([t, style, url])
    # merge neighbours with the same style/url
    merged = []
    for p in parts:
        if merged and merged[-1][1] == p[1] and merged[-1][2] == p[2]:
            merged[-1][0] += p[0]
        else:
            merged.append(p)
    out = []
    for t, style, url in merged:
        lead = t[: len(t) - len(t.lstrip())]
        trail = t[len(t.rstrip()):]
        core = t.strip()
        if not core:
            out.append(t)
            continue
        if style == "code" and not re.search(r"\w", core):
            core = esc(core)          # a lone quote or bracket in a monospace font is not code
        elif style == "code":
            core = "`" + core.replace("`", "'") + "`"
        else:
            core = esc(core)
            if style == "b" and re.search(r"\w", core):
                core = f"**{core}**"
            elif style == "i" and re.search(r"\w", core):
                core = f"*{core}*"
        if url and not core.startswith("`"):
            core = f"[{core}]({url})"
        elif url:
            core = f"[{core}]({url})"
        out.append(lead + core + trail)
    return "".join(out)


def _borderless_tables(lines, page_w, links):
    """Find tables drawn without ruling lines: rows of 2+ cells side by side.

    Lines whose vertical extents overlap form a visual row; three or more
    consecutive rows with the same column starts (and a short first cell, so
    two columns of body text never qualify) mark a table region. Inside the
    region each column is clustered into cell blocks by line spacing, and every
    block is assigned to the row whose first-column cell is vertically nearest,
    so a term centred beside a multi-line description gets the whole
    description. A region whose first column is only list markers ("1.", "-")
    is returned as list items instead.

    Returns (bbox, rows, used_line_ids, kind) with kind "table" or "list".
    """
    srt = sorted(lines, key=lambda l: (l["bbox"][1], l["bbox"][0]))
    rows, cur = [], []
    for ln in srt:
        if cur and ln["bbox"][1] < max(l["bbox"][3] for l in cur) - 1.5:
            cur.append(ln)
        else:
            if cur:
                rows.append(cur)
            cur = [ln]
    if cur:
        rows.append(cur)

    def starts(row):
        cols = []
        for x in sorted({round(l["bbox"][0]) for l in row}):
            if not cols or x - cols[-1] > 12:
                cols.append(x)
        return cols

    def is_candidate(row):
        cols = starts(row)
        if len(cols) < 2 or len(cols) > 6:
            return False
        first = [l for l in row if abs(l["bbox"][0] - cols[0]) <= 12]
        width = max(l["bbox"][2] for l in first) - cols[0]
        words = sum(len(l["raw"].split()) for l in first) / max(1, len({round(l["bbox"][1]) for l in first}))
        return width < page_w * 0.32 and words <= 6

    def col_index(x, cols):
        return min(range(len(cols)), key=lambda k: abs(cols[k] - x))

    out, i = [], 0
    while i < len(rows):
        if not is_candidate(rows[i]):
            i += 1
            continue
        cols = starts(rows[i])
        j, n_cand = i, 0
        while j < len(rows):
            c = starts(rows[j])
            if is_candidate(rows[j]) and len(c) == len(cols) and all(abs(a - b) <= 14 for a, b in zip(c, cols)):
                n_cand += 1
                j += 1
            elif len(c) == 1 and any(abs(c[0] - x) <= 14 for x in cols[1:]) and j > i:
                j += 1          # a wrapped line of a non-first cell
            else:
                break
        if n_cand < 3:
            i += 1
            continue
        lo = i
        if lo > 0:   # a wrapped line just above the first row (term centred lower)
            prev = rows[lo - 1]
            pc = starts(prev)
            if len(pc) == 1 and any(abs(pc[0] - x) <= 14 for x in cols[1:]) and \
                    rows[lo][0]["bbox"][1] - max(l["bbox"][3] for l in prev) < prev[0]["size"] * 0.6:
                lo -= 1
        header_row = None
        if lo > 0:   # a bold row just above with one piece per column is the header
            hr = rows[lo - 1]
            if len(hr) == len(cols) and all(l["bold"] > 0.9 for l in hr) and \
                    rows[lo][0]["bbox"][1] - max(l["bbox"][3] for l in hr) < hr[0]["size"] * 1.5:
                header_row = sorted(hr, key=lambda l: l["bbox"][0])
        region = [l for r in rows[lo:j] for l in r]
        anchors = sorted([l for l in region if col_index(l["bbox"][0], cols) == 0],
                         key=lambda l: l["bbox"][1])
        anchor_blocks = []
        for l in anchors:
            if anchor_blocks and l["bbox"][1] - anchor_blocks[-1][-1]["bbox"][3] < l["size"] * 0.3:
                anchor_blocks[-1].append(l)
            else:
                anchor_blocks.append([l])
        centers = [(blk[0]["bbox"][1] + blk[-1]["bbox"][3]) / 2 for blk in anchor_blocks]
        cells = [[[] for _ in cols] for _ in anchor_blocks]
        for r_i, blk in enumerate(anchor_blocks):
            cells[r_i][0] = list(blk)
        for k in range(1, len(cols)):
            col_lines = sorted([l for l in region if col_index(l["bbox"][0], cols) == k],
                               key=lambda l: l["bbox"][1])
            for l in col_lines:
                ly0, ly1 = l["bbox"][1], l["bbox"][3]
                # beside a first-column cell -> that row; otherwise the nearest row
                over = [q for q, blk in enumerate(anchor_blocks)
                        if min(ly1, blk[-1]["bbox"][3]) - max(ly0, blk[0]["bbox"][1]) > 0.3 * (ly1 - ly0)]
                cy = (ly0 + ly1) / 2
                pool = over or range(len(centers))
                r_i = min(pool, key=lambda q: abs(centers[q] - cy))
                cells[r_i][k].append(l)

        def text_of(ls):
            t = ""
            for l in sorted(ls, key=lambda l: (l["bbox"][1], l["bbox"][0])):
                t = _join_lines(t, clean_ws(_span_markup(l["spans"], links, False, False)))
            return t

        table = [[text_of(c) for c in row] for row in cells]
        used = {id(l) for l in region}
        if header_row:
            table.insert(0, [clean_ws(_span_markup(l["spans"], links, True, False)) for l in header_row])
            used |= {id(l) for l in header_row}
            region = region + header_row
        bb = (min(l["bbox"][0] for l in region), min(l["bbox"][1] for l in region),
              max(l["bbox"][2] for l in region), max(l["bbox"][3] for l in region))
        markers = [re.sub(r"[*`]", "", row[0]).strip() for row in table]
        if len(cols) == 2 and not header_row and all(LIST_MARK_ONLY.fullmatch(m) for m in markers if m) \
                and any(markers):
            out.append((bb, [row[1] for row in table], used, "list"))
        else:
            out.append((bb, table, used, "table" if header_row else "table-nohead"))
        i = j
    return out


def _join_lines(prev: str, nxt: str) -> str:
    if not prev:
        return nxt
    # soft hyphenation: "transla-" + "tion" -> "translation"
    m = re.search(r"([a-z\u00e0-\u024f]{2,})-$", prev)
    if m and re.match(r"[a-z\u00e0-\u024f]", nxt):
        return prev[:-1] + nxt
    if prev.endswith((" ", "\u200b")):
        return prev + nxt
    return prev + " " + nxt


def extract_pdf(path: Path, book: Book, opts: dict) -> None:
    import pymupdf

    doc = pymupdf.open(path)
    md = doc.metadata or {}
    book.meta["title"] = clean_ws(md.get("title") or "")
    book.meta["author"] = clean_ws(md.get("author") or "")
    skip = set(opts.get("skip_pages") or [])
    columns = int(opts.get("columns") or 1)
    want_tables = opts.get("tables", True)

    # ---- pass 1: statistics (body size, repeated page furniture, scans) ----
    size_chars: Counter = Counter()
    size_pages: dict = {}
    margin_lines: Counter = Counter()
    textless = 0
    for pno in range(doc.page_count):
        if pno + 1 in skip:
            continue
        page = doc[pno]
        h = page.rect.height
        d = page.get_text("dict")
        chars = 0
        for b in d["blocks"]:
            if b["type"] != 0:
                continue
            for l in b["lines"]:
                txt = "".join(s["text"] for s in l["spans"]).strip()
                chars += len(txt)
                for s in l["spans"]:
                    if s["text"].strip() and not _is_mono(s):
                        sz = round(s["size"] * 2) / 2
                        size_chars[sz] += len(s["text"])
                        size_pages.setdefault(sz, set()).add(pno)
                y0, y1 = l["bbox"][1], l["bbox"][3]
                if txt and (y1 < h * 0.085 or y0 > h * 0.915):
                    margin_lines[re.sub(r"\d+", "#", txt.lower())] += 1
        if chars < 20:
            textless += 1
    n_pages = max(1, doc.page_count - len(skip))
    if textless > n_pages * 0.5:
        raise SystemExit(
            f"{path.name}: {textless} of {n_pages} pages have no text layer. "
            "This is a scanned PDF - run OCR first (e.g. `ocrmypdf --language eng in.pdf out.pdf`) "
            "and extract the OCR'd file.")
    body = size_chars.most_common(1)[0][0] if size_chars else 10.0
    furniture = {k for k, v in margin_lines.items()
                 if v >= max(3, n_pages * 0.2) or re.fullmatch(r"[#\s\-–—|/.ivxlc]*", k)}
    # sizes used for headings on several pages (cover/title-page sizes excluded)
    heading_sizes = sorted({s for s, n in size_chars.items()
                            if s >= body * 1.15 and n >= 3
                            and len(size_pages.get(s, ())) >= min(2, n_pages)}, reverse=True)

    def heading_level(size: float) -> int:
        for i, s in enumerate(heading_sizes):
            if size >= s - 0.25:
                return min(i + 1, 4)
        return 0

    # ---- chapter boundaries from the outline ----
    toc = doc.get_toc(simple=False)
    level = opts.get("chapter_level")
    if not level and toc:
        lv_counts = Counter(t[0] for t in toc)
        level = next((lv for lv in sorted(lv_counts) if lv_counts[lv] >= 2), 1)
    marks = []   # (page_index, y, title)
    for t in toc:
        if t[0] != level or t[2] < 1:
            continue
        dest = t[3] if len(t) > 3 else {}
        to = dest.get("to") if isinstance(dest, dict) else None
        y = 0.0
        if to is not None:
            try:
                # PyMuPDF reports outline targets in top-left page coordinates
                y = min(max(0.0, float(to[1])), doc[t[2] - 1].rect.height)
            except Exception:
                y = 0.0
        marks.append((t[2] - 1, y, fix_ligatures(t[1]).strip()))
    marks.sort()
    use_outline = len(marks) >= 2
    if not use_outline:
        book.notes.append("PDF has no usable outline: chapters are split at the largest headings.")
    if marks and marks[0][0] > 0:
        book.chapter("")   # front matter before the first outline chapter
    mark_i = 0
    pending_title = None

    def norm_t(s: str) -> str:
        return re.sub(r"[\W_]+", "", s.lower())

    # ---- pass 2: content ----
    last = None          # last emitted block (for cross-line merging)
    last_line = None     # geometry of the last line added to `last`
    for pno in range(doc.page_count):
        if pno + 1 in skip:
            continue
        page = doc[pno]
        pw, ph = page.rect.width, page.rect.height
        d = page.get_text("dict")
        links = [((l["from"].x0, l["from"].y0, l["from"].x1, l["from"].y1), l["uri"])
                 for l in page.get_links() if l.get("uri")]

        lines = []
        for b in d["blocks"]:
            if b["type"] != 0:
                continue
            for l in b["lines"]:
                if l.get("dir", (1, 0))[0] < 0.9 and abs(l.get("dir", (1, 0))[1]) > 0.1:
                    continue   # rotated text (margins, watermarks)
                spans = []
                for s in l["spans"]:
                    if not s["text"]:
                        continue
                    spans.append({"text": s["text"], "size": s["size"], "mono": _is_mono(s),
                                  "bold": _is_bold(s), "italic": _is_italic(s),
                                  "bbox": s["bbox"]})
                raw = "".join(s["text"] for s in spans)
                if not raw.strip():
                    continue
                y0, y1 = l["bbox"][1], l["bbox"][3]
                if (y1 < ph * 0.085 or y0 > ph * 0.915) and \
                        re.sub(r"\d+", "#", raw.strip().lower()) in furniture:
                    continue
                n = sum(len(s["text"].strip()) for s in spans) or 1
                lines.append({
                    "bbox": tuple(l["bbox"]), "spans": spans, "raw": raw,
                    "size": max(spans, key=lambda s: len(s["text"].strip()))["size"],
                    "mono": sum(len(s["text"].strip()) for s in spans if s["mono"]) / n,
                    "bold": sum(len(s["text"].strip()) for s in spans if s["bold"]) / n,
                    "italic": sum(len(s["text"].strip()) for s in spans if s["italic"]) / n,
                })

        # ---- bullets drawn as shapes, or as a lone glyph left of the text
        page_drawings = page.get_drawings()
        dots = [d_["rect"] for d_ in page_drawings
                if 1.5 < d_["rect"].width < 7.5 and 1.5 < d_["rect"].height < 7.5 and d_.get("fill") is not None]
        lone = [ln for ln in lines if ln["raw"].strip() in ("•", "●", "◦", "▪", "■",
                                                            "‣", "⁃", "·", "-", "*", "–")]
        marks_left = [tuple(r) for r in dots] + [ln["bbox"] for ln in lone]
        lines = [ln for ln in lines if ln not in lone]
        for ln in lines:
            x0, y0, x1, y1 = ln["bbox"]
            for m in marks_left:
                cy = (m[1] + m[3]) / 2
                if m[2] <= x0 + 1 and x0 - m[0] < 28 and y0 - 1 <= cy <= y1 + 1:
                    ln["bullet"] = True
                    break

        # ---- figures: raster images + vector drawings that are not text boxes
        regions = []
        for info in page.get_image_info():
            r = tuple(info["bbox"])
            w, h = r[2] - r[0], r[3] - r[1]
            if w < 24 or h < 24:
                continue
            if _rect_area(r) > pw * ph * 0.85 and lines:
                continue   # full-page background
            regions.append([*r, 99])
        draw = []
        for dr in page_drawings:
            r = dr["rect"]
            if r.width < 1 and r.height < 1:
                continue
            if r.width * r.height > pw * ph * 0.5:
                continue   # page background / full-page frame, not a figure
            draw.append([r.x0, r.y0, r.x1, r.y1, 1])      # count separate shapes, not path items
        for c in _merge_rects(draw):
            r = tuple(c[:4])
            w, h = r[2] - r[0], r[3] - r[1]
            if w < 15 or h < 15 or _rect_area(r) < 900:
                continue
            inside = [ln for ln in lines if _inside(ln["bbox"], r, 0.7)]
            text_area = sum(_rect_area(ln["bbox"]) for ln in inside)
            ratio = text_area / max(_rect_area(r), 1)
            shapes = c[4]
            mono = sum(ln["mono"] for ln in inside) / len(inside) if inside else 0
            prose = any(len(ln["raw"].split()) >= 6 for ln in inside)
            if r[0] < 2 or r[1] < 2 or r[2] > pw - 2 or r[3] > ph - 2:
                continue   # bleeds off the page: decoration
            crossing = [ln for ln in lines if ln not in inside
                        and _inter(ln["bbox"], r) > 0.1 * min(_rect_area(ln["bbox"]), _rect_area(r))]
            if crossing:
                continue   # highlight / chip behind running text
            if not inside and shapes >= 2 and _rect_area(r) > 2500:
                regions.append([*r, shapes])
            elif inside and shapes >= 4 and ratio < 0.25 and len(inside) <= 12                     and mono < 0.5 and not prose:
                regions.append([*r, shapes])      # diagram with a few short labels
            # else: a box or decoration behind text (code box, callout, banner):
            # the text is extracted normally and the shapes are dropped
        figures = [tuple(r[:4]) for r in _merge_rects(regions, pad=2)]

        tables = []
        if want_tables:
            try:
                for t in page.find_tables().tables:
                    rows = t.extract()
                    if t.row_count < 2 or t.col_count < 2:
                        continue
                    bb = tuple(t.bbox)
                    inside = [ln for ln in lines if _inside(ln["bbox"], bb, 0.6)]
                    if inside and sum(ln["mono"] for ln in inside) / len(inside) > 0.6:
                        continue   # a code box, not a table
                    if any(ln not in inside and _inter(ln["bbox"], bb) > 0.1 * _rect_area(ln["bbox"])
                           for ln in lines):
                        continue   # boxes behind running text (key caps, highlights), not a table
                    cells_all = [c for row in rows for c in row]
                    if sum(1 for c in cells_all if not (c or "").strip()) > 0.4 * len(cells_all):
                        continue   # mostly empty grid
                    cells = [[clean_ws(fix_ligatures(c or "")).replace("\n", " ") for c in row]
                             for row in rows]
                    filled = sum(1 for row in cells for c in row if c)
                    if filled < 3:
                        continue
                    tables.append((bb, cells))
            except Exception as e:   # pragma: no cover - pymupdf version quirks
                book.notes.append(f"table detection failed on page {pno + 1}: {e}")

        # assign lines inside figures/tables to them
        def col_of(r):
            if columns < 2:
                return 0
            return min(columns - 1, int(((r[0] + r[2]) / 2) / (pw / columns)))

        events = []   # (column, y, x, kind, payload)
        for r in figures:
            labels = [ln["raw"].strip() for ln in lines if _inside(ln["bbox"], r, 0.6)]
            events.append((col_of(r), r[1], r[0], "figure", (r, labels)))
        for bb, cells in tables:
            events.append((col_of(bb), bb[1], bb[0], "table", cells))
        taken = [r for r in figures] + [t[0] for t in tables]
        free = [ln for ln in lines if not any(_inside(ln["bbox"], r, 0.6) for r in taken)]
        if columns < 2 and want_tables:
            for bb, cells, used, kind in _borderless_tables(free, pw, links):
                events.append((0, bb[1], bb[0], {"list": "items"}.get(kind, kind), cells))
                free = [ln for ln in free if id(ln) not in used]
        for ln in free:
            events.append((col_of(ln["bbox"]), ln["bbox"][1], ln["bbox"][0], "line", ln))
        events.sort(key=lambda e: (e[0], round(e[1] / 3), e[2]))

        for _col, y, x, kind, payload in events:
            # chapter boundary from the outline
            while use_outline and mark_i < len(marks) and (
                    marks[mark_i][0] < pno or (marks[mark_i][0] == pno and y >= marks[mark_i][1] - 40)):
                book.chapter(marks[mark_i][2])
                pending_title = norm_t(marks[mark_i][2])
                mark_i += 1
                last = last_line = None
            if kind == "figure":
                r, labels = payload
                clip = pymupdf.Rect(r[0] - 3, r[1] - 3, r[2] + 3, r[3] + 3) & page.rect
                pix = page.get_pixmap(clip=clip, dpi=int(opts.get("dpi") or 200))
                f = book.save_image(pix.tobytes("png"), "png")
                book.add({"type": "figure", "file": f, "alt": "", "labels": labels[:40]})
                last = last_line = None
                continue
            if kind == "table":
                book.add({"type": "table", "rows": payload, "header": True})
                last = last_line = None
                continue
            if kind == "table-nohead":
                book.add({"type": "table", "rows": payload, "header": False})
                last = last_line = None
                continue
            if kind == "items":
                for t in payload:
                    book.add({"type": "item", "text": t, "ordered": True, "depth": 0})
                last = last_line = None
                continue

            ln = payload
            raw = fix_ligatures(ln["raw"])
            size = ln["size"]
            is_code = ln["mono"] >= 0.8
            lvl = 0 if is_code else heading_level(size)
            text_line = raw.strip()
            # heading lines that repeat the outline title are the chapter title itself
            if pending_title is not None:
                nl = norm_t(text_line)
                if lvl and nl and (nl in pending_title or pending_title in nl):
                    last_line = ln
                    ln["_page"] = pno
                    continue
                pending_title = None
            if lvl == 1 and not use_outline:
                book.chapter(re.sub(r"\s+", " ", text_line))
                last = last_line = None
                continue

            whole_bold = ln["bold"] > 0.9
            whole_italic = ln["italic"] > 0.9
            gap = (ln["bbox"][1] - last_line["bbox"][3]) if last_line is not None else 999
            new_page = last_line is not None and last_line.get("_page") != pno
            ln["_page"] = pno

            if is_code:
                indent = ""
                if last and last["type"] == "code" and last_line is not None:
                    x0 = last.get("_x0", ln["bbox"][0])
                    cw = max(size * 0.6, 1)
                    n = int(round((ln["bbox"][0] - x0) / cw))
                    if n > 0 and not raw.startswith(" "):
                        indent = " " * n
                if last and last["type"] == "code" and (new_page or gap < size * 2.6):
                    blanks = 0 if new_page else max(0, int(round(gap / (size * 1.25))) - 0)
                    last["text"] += "\n" * (1 + (blanks if gap > size * 0.9 else 0)) + indent + raw.rstrip()
                    last_line = ln
                    continue
                last = {"type": "code", "text": raw.rstrip(), "_x0": ln["bbox"][0]}
                book.add(last)
                last_line = ln
                continue

            marked = _span_markup(ln["spans"], links, whole_bold, whole_italic)
            marked = clean_ws(marked)
            if lvl:
                if last and last["type"] == "heading" and not last.get("_consumed") \
                        and last.get("level") == lvl and gap < size * 0.8:
                    last["text"] = _join_lines(last["text"], marked)
                else:
                    last = {"type": "heading", "level": lvl, "text": marked}
                    book.add(last)
                last_line = ln
                continue

            m = LIST_RE.match(text_line)
            col_left = ln["bbox"][0]
            if (m and not whole_bold) or ln.get("bullet"):
                ordered = bool(m and m.group(2))
                body_text = clean_ws(LIST_RE.sub("", marked, count=1)) if m else marked
                last = {"type": "item", "text": body_text, "ordered": ordered, "depth": 0,
                        "_x0": col_left}
                book.add(last)
                last_line = ln
                continue

            cont = False
            if last and last["type"] in ("para", "item", "quote") and last_line is not None:
                prev_txt = last["text"]
                if new_page:
                    cont = not prev_txt.rstrip().endswith(TERMINAL) and \
                        bool(re.match(r"[a-z\u00e0-\u024f(]", text_line))
                else:
                    same_par = gap < size * 0.55
                    indent_start = col_left > last_line["bbox"][0] + size * 0.9 and last["type"] == "para"
                    short_prev = last_line["bbox"][2] < (last.get("_x1max", last_line["bbox"][2]) - size * 3)
                    ended = prev_txt.rstrip().endswith(TERMINAL)
                    cont = same_par and not (indent_start and ended) and not (short_prev and ended and indent_start)
                    if last["type"] == "item" and col_left < last.get("_x0", 0) - 1:
                        cont = False
            if cont:
                last["text"] = _join_lines(last["text"], marked)
                last["_x1max"] = max(last.get("_x1max", 0), ln["bbox"][2])
                last_line = ln
                continue
            last = {"type": "para", "text": marked, "_x1max": ln["bbox"][2]}
            if whole_bold and len(text_line.split()) <= 12 and not text_line.endswith(TERMINAL[:3]):
                last = {"type": "heading", "level": 4, "text": marked}
            book.add(last)
            last_line = ln

    for ch in book.chapters:
        for b in ch["blocks"]:
            for k in [k for k in b if k.startswith("_")]:
                del b[k]
        ch["blocks"] = [b for b in ch["blocks"] if b.get("type")]
        if not ch["title"]:
            first = next((b for b in ch["blocks"] if b["type"] == "heading"), None)
            if first and ch["blocks"] and ch["blocks"][0] is first:
                ch["title"] = first["text"]
                ch["blocks"].pop(0)
    if not book.meta["title"] and book.chapters and book.chapters[0]["title"]:
        book.meta["title"] = book.chapters[0]["title"]


# ---------------------------------------------------------------------------
# HTML / EPUB
# ---------------------------------------------------------------------------

BLOCK_TAGS = {"p", "div", "section", "article", "header", "footer", "aside", "main",
              "body", "li", "dd", "dt", "blockquote", "figure", "figcaption",
              "td", "th", "tr", "table", "pre", "ul", "ol", "dl", "hr", "br",
              "h1", "h2", "h3", "h4", "h5", "h6", "caption", "center", "address"}
SKIP_TAGS = {"script", "style", "head", "title", "nav", "noscript", "template", "rt", "rp", "svg:title"}
CODE_TAGS = {"code", "kbd", "samp", "tt", "var"}


class _HTMLBlocks(HTMLParser):
    def __init__(self, book: Book, resolve_img, heading_split: int | None):
        super().__init__(convert_charrefs=True)
        self.book = book
        self.resolve_img = resolve_img
        self.split = heading_split
        self.buf: list[str] = []
        self.kind = "para"
        self.level = 0
        self.skip = 0
        self.pre = 0
        self.pre_buf: list[str] = []
        self.code = 0
        self.lists: list[bool] = []
        self.quote = 0
        self.links: list[str | None] = []
        self.table: list[list[str]] | None = None
        self.row: list[str] | None = None
        self.cell: list[str] | None = None
        self.header_row = False
        self.figcap = 0
        self.first_heading: str | None = None

    # -- helpers
    def _target(self):
        return self.cell if self.cell is not None else self.buf

    def flush(self):
        text = clean_ws("".join(self.buf))
        self.buf = []
        if not text:
            return
        text = re.sub(r"\*\*\s*\*\*", "", text)
        if self.kind == "heading":
            if self.first_heading is None:
                self.first_heading = text
            if self.split and self.level <= self.split:
                self.book.chapter(re.sub(r"\\([*`\[\]])", r"\1", text) if False else text)
                return
            self.book.add({"type": "heading", "level": self.level, "text": text})
        elif self.figcap:
            self.book.add({"type": "caption", "text": text})
        elif self.kind == "item":
            self.book.add({"type": "item", "text": text, "ordered": bool(self.lists and self.lists[-1]),
                           "depth": max(0, len(self.lists) - 1)})
        elif self.quote:
            self.book.add({"type": "quote", "text": text})
        else:
            self.book.add({"type": "para", "text": text})

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        a = dict(attrs)
        if tag in SKIP_TAGS or a.get("hidden") is not None or \
                "display:none" in (a.get("style") or "").replace(" ", ""):
            if tag not in ("br", "hr", "img"):
                self.skip += 1
            return
        if self.skip:
            return
        if self.pre:
            if tag == "br":
                self.pre_buf.append("\n")
            return
        if tag == "pre":
            self.flush()
            self.pre += 1
            self.pre_buf = []
            return
        if tag in ("img", "image"):
            src = a.get("src") or a.get("xlink:href") or a.get("href")
            if src:
                f = self.resolve_img(src)
                if f:
                    self.flush()
                    self.book.add({"type": "figure", "file": f, "alt": clean_ws(a.get("alt") or ""),
                                   "labels": []})
            return
        if tag == "br":
            self._target().append(" ")
            return
        if tag == "hr":
            self.flush()
            self.book.add({"type": "rule"})
            return
        if tag == "table":
            self.flush()
            self.table = []
            return
        if tag == "tr" and self.table is not None:
            self.row = []
            return
        if tag in ("td", "th") and self.row is not None:
            self.cell = []
            if tag == "th" and not self.table:
                self.header_row = True
            return
        if tag in ("ul", "ol"):
            self.flush()
            self.lists.append(tag == "ol")
            return
        if tag == "li":
            self.flush()
            self.kind = "item"
            return
        if tag == "blockquote":
            self.flush()
            self.quote += 1
            return
        if tag == "figcaption":
            self.flush()
            self.figcap += 1
            return
        if re.fullmatch(r"h[1-6]", tag):
            self.flush()
            self.kind, self.level = "heading", int(tag[1])
            return
        if tag in BLOCK_TAGS:
            self.flush()
            if self.kind == "heading":
                self.kind = "para"
            return
        t = self._target()
        if tag in CODE_TAGS:
            self.code += 1
            if self.code == 1:
                t.append("\x01")
        elif tag in ("b", "strong") and not self.code:
            t.append("**")
        elif tag in ("i", "em", "cite", "dfn") and not self.code:
            t.append("*")
        elif tag == "a":
            href = a.get("href") or ""
            ext = href if re.match(r"(https?|mailto|ftp):", href) else None
            self.links.append(ext)
            if ext:
                t.append("[")
        elif tag == "sup" and not self.code:
            t.append("^")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in SKIP_TAGS:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if self.pre:
            if tag == "pre":
                self.pre -= 1
                if not self.pre:
                    code = "".join(self.pre_buf).strip("\n")
                    if self.cell is not None:
                        self.cell.append("`" + clean_ws(code) + "`")
                    else:
                        self.book.add({"type": "code", "text": code})
                    self.pre_buf = []
            return
        t = self._target()
        if tag in CODE_TAGS:
            self.code = max(0, self.code - 1)
            if self.code == 0:
                t.append("\x02")
            return
        if self.code:
            return
        if tag in ("b", "strong"):
            t.append("**")
        elif tag in ("i", "em", "cite", "dfn"):
            t.append("*")
        elif tag == "a":
            ext = self.links.pop() if self.links else None
            if ext:
                t.append(f"]({ext})")
        elif tag in ("td", "th") and self.cell is not None and self.row is not None:
            self.row.append(_finish_inline(clean_ws("".join(self.cell))))
            self.cell = None
        elif tag == "tr" and self.row is not None and self.table is not None:
            if any(c for c in self.row):
                self.table.append(self.row)
            self.row = None
        elif tag == "table" and self.table is not None:
            rows = self.table
            self.table = None
            if rows:
                width = max(len(r) for r in rows)
                rows = [r + [""] * (width - len(r)) for r in rows]
                self.book.add({"type": "table", "rows": rows, "header": self.header_row})
            self.header_row = False
        elif tag in ("ul", "ol"):
            self.flush()
            if self.lists:
                self.lists.pop()
            self.kind = "item" if self.lists else "para"
        elif tag == "li":
            self.flush()
            self.kind = "item" if self.lists else "para"
        elif tag == "blockquote":
            self.flush()
            self.quote = max(0, self.quote - 1)
        elif tag == "figcaption":
            self.flush()
            self.figcap = max(0, self.figcap - 1)
        elif re.fullmatch(r"h[1-6]", tag):
            self.flush()
            self.kind = "item" if self.lists else "para"
        elif tag in BLOCK_TAGS:
            self.flush()

    def handle_data(self, data):
        if self.skip:
            return
        if self.pre:
            self.pre_buf.append(data)
            return
        t = self._target()
        if self.code:
            t.append(data.replace("`", "'"))
        else:
            t.append(esc(data))

    def close(self):
        super().close()
        self.flush()


def _finish_inline(text: str) -> str:
    text = text.replace("\x01", "`").replace("\x02", "`")
    text = re.sub(r"``", "", text)
    text = re.sub(r"\*\*(\s*)\*\*", r"", text)          # empty bold
    text = re.sub(r"\[\s*\]\([^)]*\)", "", text)
    return clean_ws(text)


def _finalize_html_blocks(book: Book) -> None:
    for ch in book.chapters:
        for b in ch["blocks"]:
            if "text" in b and b["type"] != "code":
                b["text"] = _finish_inline(b["text"])
        ch["title"] = re.sub(r"\\([*`\[\]])", r"\1", _finish_inline(ch["title"]))
        ch["title"] = re.sub(r"\*\*|`", "", ch["title"]).strip("* ")


def extract_html_text(text: str, book: Book, base_dir: Path | None, split_level=None) -> None:
    def resolve(src):
        if src.startswith("data:"):
            m = re.match(r"data:image/(\w+);base64,(.*)", src, re.S)
            if m:
                import base64
                return book.save_image(base64.b64decode(m.group(2)), m.group(1))
            return None
        if base_dir is None or re.match(r"https?:", src):
            book.notes.append(f"remote image not downloaded: {src[:80]}")
            return None
        p = (base_dir / html.unescape(src.split("#")[0].split("?")[0])).resolve()
        if p.exists():
            return book.save_image(p.read_bytes(), p.suffix)
        book.notes.append(f"image not found: {src}")
        return None

    if split_level is None:
        h1 = len(re.findall(r"<h1[\s>]", text, re.I))
        split_level = 1 if h1 >= 2 else (2 if len(re.findall(r"<h2[\s>]", text, re.I)) >= 2 else None)
    lm = re.search(r"<html[^>]*lang=[\"']([\w-]+)", text, re.I)
    if lm:
        book.meta["language"] = lm.group(1)
    m = re.search(r"<title>(.*?)</title>", text, re.I | re.S)
    if m and not book.meta["title"]:
        book.meta["title"] = clean_ws(html.unescape(re.sub("<[^>]+>", "", m.group(1))))
    p = _HTMLBlocks(book, resolve, split_level)
    p.feed(text)
    p.close()
    _finalize_html_blocks(book)


def extract_epub(path: Path, book: Book, opts: dict) -> None:
    z = zipfile.ZipFile(path)
    names = set(z.namelist())
    if "META-INF/encryption.xml" in names:
        enc = z.read("META-INF/encryption.xml").decode("utf-8", "replace")
        if re.search(r"<(?:\w+:)?CipherReference[^>]+URI=\"[^\"]+\.(x?html?|xml)\"", enc):
            raise SystemExit(f"{path.name} is DRM-protected; only DRM-free EPUBs can be translated.")
    container = z.read("META-INF/container.xml").decode("utf-8", "replace")
    opf_path = re.search(r'full-path="([^"]+)"', container).group(1)
    opf = z.read(opf_path).decode("utf-8", "replace")
    base = posixpath.dirname(opf_path)

    def meta(tag):
        m = re.search(rf"<dc:{tag}[^>]*>(.*?)</dc:{tag}>", opf, re.S | re.I)
        return clean_ws(html.unescape(re.sub("<[^>]+>", "", m.group(1)))) if m else ""
    book.meta["title"], book.meta["author"] = meta("title"), meta("creator")
    book.meta["language"] = meta("language")

    manifest = {}
    for m in re.finditer(r"<item\b([^>]*)/?>", opf):
        attrs = dict(re.findall(r'([\w:-]+)="([^"]*)"', m.group(1)))
        if "id" in attrs and "href" in attrs:
            manifest[attrs["id"]] = (attrs["href"], attrs.get("media-type", ""), attrs.get("properties", ""))
    spine = re.findall(r'<itemref\b[^>]*idref="([^"]+)"[^>]*>', opf)

    for idref in spine:
        if idref not in manifest:
            continue
        href, mtype, props = manifest[idref]
        if "nav" in props.split():
            continue
        doc_path = posixpath.normpath(posixpath.join(base, html.unescape(href)))
        if doc_path not in names:
            continue
        text = z.read(doc_path).decode("utf-8", "replace")
        doc_dir = posixpath.dirname(doc_path)

        def resolve(src, doc_dir=doc_dir):
            if re.match(r"https?:", src):
                return None
            p = posixpath.normpath(posixpath.join(doc_dir, html.unescape(src.split("#")[0])))
            if p in names:
                return book.save_image(z.read(p), posixpath.splitext(p)[1])
            book.notes.append(f"image not found in EPUB: {p}")
            return None

        ch = book.chapter("")
        p = _HTMLBlocks(book, resolve, None)
        p.feed(text)
        p.close()
        _finalize_html_blocks(book)
        blocks = ch["blocks"]
        if blocks and blocks[0]["type"] == "heading":
            ch["title"] = re.sub(r"\\([*`\[\]])", r"\1", re.sub(r"\*\*|`", "", blocks[0]["text"]))
            blocks.pop(0)
    # merge text-less chapters without titles (cover pages, blank separators)
    merged = []
    for ch in book.chapters:
        if merged and not ch["title"] and not any(b["type"] != "figure" for b in ch["blocks"]) \
                and not merged[-1]["title"]:
            merged[-1]["blocks"].extend(ch["blocks"])
        else:
            merged.append(ch)
    book.chapters = merged


# ---------------------------------------------------------------------------
# DOCX
# ---------------------------------------------------------------------------

def extract_docx(path: Path, book: Book, opts: dict) -> None:
    from docx import Document
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    doc = Document(str(path))
    cp = doc.core_properties
    book.meta["title"], book.meta["author"] = clean_ws(cp.title or ""), clean_ws(cp.author or "")
    if book.meta["author"].lower() in ("python-docx", "author", "user", "administrator"):
        book.meta["author"] = ""
    book.meta["language"] = cp.language or ""
    split = opts.get("chapter_level") or None
    styles_h1 = sum(1 for p in doc.paragraphs if (p.style.name or "").lower() in ("heading 1", "title"))
    if split is None:
        split = 1 if styles_h1 >= 2 else (2 if sum(1 for p in doc.paragraphs
                                                   if (p.style.name or "").lower() == "heading 2") >= 2 else 0)

    def run_mono(run):
        names = [run.font.name or "", (run.style.font.name if run.style is not None else "") or ""]
        rf = run._r.find(qn("w:rPr"))
        if rf is not None and rf.find(qn("w:rFonts")) is not None:
            names.append(rf.find(qn("w:rFonts")).get(qn("w:ascii")) or "")
        return any(h in n.lower().replace(" ", "") for n in names for h in MONO_HINTS)

    def para_text(p):
        out = []
        for item in p.iter_inner_content():
            if hasattr(item, "runs") and hasattr(item, "url"):    # Hyperlink
                inner = esc("".join(r.text for r in item.runs))
                url = item.url or ""
                out.append(f"[{inner}]({url})" if url.startswith(("http", "mailto")) and inner.strip() else inner)
                continue
            t = item.text
            if not t:
                continue
            if run_mono(item):
                out.append("`" + t.replace("`", "'") + "`")
            elif item.bold and t.strip():
                out.append(f"**{esc(t)}**")
            elif item.italic and t.strip():
                out.append(f"*{esc(t)}*")
            else:
                out.append(esc(t))
        txt = "".join(out)
        txt = re.sub(r"\*\*(\s*)\*\*", r"\1", txt)
        txt = re.sub(r"`(\s*)`", r"\1", txt)
        return clean_ws(txt)

    def images(p):
        for blip in p._p.iter(qn("a:blip")):
            rid = blip.get(qn("r:embed"))
            part = doc.part.related_parts.get(rid) if rid else None
            if part is not None:
                ext = posixpath.splitext(part.partname)[1]
                yield book.save_image(part.blob, ext)

    code_buf: list[str] = []

    def flush_code():
        if code_buf:
            book.add({"type": "code", "text": "\n".join(code_buf)})
            code_buf.clear()

    for el in doc.element.body.iterchildren():
        if el.tag == qn("w:tbl"):
            flush_code()
            t = Table(el, doc)
            rows = []
            for r in t.rows:
                row = []
                for c in r.cells:
                    row.append(clean_ws(" ".join(para_text(p) for p in c.paragraphs)))
                rows.append(row)
            # merged cells repeat; keep as-is (translations are per cell)
            if rows:
                book.add({"type": "table", "rows": rows, "header": True})
            continue
        if el.tag != qn("w:p"):
            continue
        p = Paragraph(el, doc)
        for f in images(p):
            flush_code()
            book.add({"type": "figure", "file": f, "alt": "", "labels": []})
        style = (p.style.name or "").lower()
        raw = p.text
        if not raw.strip():
            continue
        runs = [r for r in p.runs if r.text.strip()]
        is_code = ("code" in style or "preformatted" in style or "source" in style or
                   (runs and all(run_mono(r) for r in runs)))
        if is_code:
            code_buf.append(raw.rstrip())
            continue
        flush_code()
        text = para_text(p)
        m = re.match(r"heading (\d)", style)
        if style == "title" or m:
            lvl = 1 if style == "title" else int(m.group(1))
            plain = re.sub(r"\\([*`\[\]])", r"\1", re.sub(r"\*\*|`", "", text))
            if style == "title" and not book.meta["title"]:
                book.meta["title"] = plain
            if split and lvl <= split:
                book.chapter(plain)
            else:
                book.add({"type": "heading", "level": lvl, "text": text})
        elif el.find(qn("w:pPr")) is not None and el.find(qn("w:pPr")).find(qn("w:numPr")) is not None \
                or "list" in style:
            ordered = "number" in style
            ilvl = el.find(qn("w:pPr")).find(qn("w:numPr"))
            depth = 0
            if ilvl is not None and ilvl.find(qn("w:ilvl")) is not None:
                depth = int(ilvl.find(qn("w:ilvl")).get(qn("w:val")) or 0)
            book.add({"type": "item", "text": text, "ordered": ordered, "depth": depth})
        elif "quote" in style:
            book.add({"type": "quote", "text": text})
        elif "caption" in style:
            book.add({"type": "caption", "text": text})
        else:
            book.add({"type": "para", "text": text})
    flush_code()


# ---------------------------------------------------------------------------
# Markdown / plain text
# ---------------------------------------------------------------------------

def extract_markdown(path: Path, book: Book, opts: dict) -> None:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        end = next((i for i in range(1, len(lines)) if lines[i].strip() in ("---", "...")), None)
        if end:
            for l in lines[1:end]:
                m = re.match(r"(title|author|language|lang)\s*:\s*[\"']?(.*?)[\"']?\s*$", l, re.I)
                if m:
                    book.meta[m.group(1).lower()] = m.group(2)
            lines = lines[end + 1:]
    heads = [len(m.group(1)) for l in lines for m in [re.match(r"(#{1,6})\s", l)] if m]
    split = opts.get("chapter_level")
    if split is None:
        split = 1 if heads.count(1) >= 2 else (2 if heads.count(2) >= 2 and heads.count(1) <= 1 else 0)
        if heads.count(1) == 1 and not book.meta["title"]:
            book.meta["title"] = next(re.sub(r"^#\s+", "", l).strip() for l in lines if re.match(r"#\s", l))

    para: list[str] = []
    base = path.parent

    def flush():
        if para:
            t = " ".join(s.strip() for s in para)
            book.add({"type": "para", "text": _md_inline(t)})
            para.clear()

    i = 0
    while i < len(lines):
        l = lines[i]
        s = l.strip()
        fence = re.match(r"^(\s*)(```+|~~~+)", l)
        if fence:
            flush()
            mark = fence.group(2)
            buf = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith(mark[:3]):
                buf.append(lines[i])
                i += 1
            book.add({"type": "code", "text": "\n".join(buf)})
            i += 1
            continue
        if not s:
            flush()
            i += 1
            continue
        m = re.match(r"^(#{1,6})\s+(.*?)\s*#*\s*$", l)
        if m:
            flush()
            lvl, t = len(m.group(1)), m.group(2)
            if split and lvl <= split:
                book.chapter(re.sub(r"[*`]", "", t))
            elif not (lvl == 1 and heads.count(1) == 1 and t == book.meta["title"]):
                book.add({"type": "heading", "level": lvl, "text": _md_inline(t)})
            i += 1
            continue
        if i + 1 < len(lines) and re.fullmatch(r"=+|-+", lines[i + 1].strip()) and not para and s:
            lvl = 1 if lines[i + 1].strip()[0] == "=" else 2
            if split and lvl <= split:
                book.chapter(s)
            else:
                book.add({"type": "heading", "level": lvl, "text": _md_inline(s)})
            i += 2
            continue
        if re.fullmatch(r"(\*\s*){3,}|(-\s*){3,}|(_\s*){3,}", s):
            flush()
            book.add({"type": "rule"})
            i += 1
            continue
        img = re.fullmatch(r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)", s)
        if img:
            flush()
            src = img.group(2)
            p = (base / src).resolve()
            if p.exists():
                f = book.save_image(p.read_bytes(), p.suffix)
                book.add({"type": "figure", "file": f, "alt": img.group(1), "labels": []})
            else:
                book.notes.append(f"image not found: {src}")
            i += 1
            continue
        if s.startswith("|") and i + 1 < len(lines) and re.fullmatch(r"\|?\s*:?-{2,}.*", lines[i + 1].strip()):
            flush()
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                row = lines[i].strip().strip("|")
                if not re.fullmatch(r"[\s:|\-]+", row):
                    rows.append([_md_inline(c.strip()) for c in re.split(r"(?<!\\)\|", row)])
                i += 1
            book.add({"type": "table", "rows": rows, "header": True})
            continue
        lm = re.match(r"^(\s*)([-*+]|\d{1,3}[.)])\s+(.*)", l)
        if lm:
            flush()
            depth = len(lm.group(1).expandtabs(4)) // 2
            buf = [lm.group(3)]
            i += 1
            while i < len(lines) and lines[i].strip() and not re.match(r"^\s*([-*+]|\d{1,3}[.)])\s+", lines[i]) \
                    and lines[i].startswith((" ", "\t")):
                buf.append(lines[i].strip())
                i += 1
            book.add({"type": "item", "text": _md_inline(" ".join(buf)),
                      "ordered": lm.group(2)[0].isdigit(), "depth": min(depth, 3)})
            continue
        if s.startswith(">"):
            flush()
            buf = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                buf.append(lines[i].strip().lstrip(">").strip())
                i += 1
            book.add({"type": "quote", "text": _md_inline(" ".join(b for b in buf if b))})
            continue
        if l.startswith("    ") and not para:
            buf = []
            while i < len(lines) and (lines[i].startswith("    ") or not lines[i].strip()):
                buf.append(lines[i][4:])
                i += 1
            book.add({"type": "code", "text": "\n".join(buf).rstrip("\n")})
            continue
        para.append(l)
        i += 1
    flush()


def _md_inline(t: str) -> str:
    """Normalise Markdown inline syntax to the block model's subset."""
    t = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", t)              # inline images -> alt
    t = re.sub(r"__(.+?)__", r"**\1**", t)
    t = re.sub(r"(?<![\w*])_(?!_)(.+?)(?<!_)_(?![\w*])", r"*\1*", t)
    t = re.sub(r"<(https?://[^>]+)>", r"[\1](\1)", t)
    return clean_ws(t)


CHAPTER_LINE = re.compile(
    r"^\s*(chapter|part|book|prologue|epilogue|introduction|preface|cap[ií]tulo|chapitre|kapitel|"
    r"capitolo|hoofdstuk|rozdzia[lł]|глава|часть|الفصل|الباب|פרק|第.{1,8}[章回节節部])\b.{0,80}$",
    re.I)


def extract_text(path: Path, book: Book, opts: dict) -> None:
    text = path.read_text(encoding="utf-8-sig", errors="replace").replace("\r\n", "\n")
    blocks = re.split(r"\n\s*\n", text) if "\n\n" in text else text.split("\n")
    for blk in blocks:
        s = clean_ws(blk.replace("\n", " "))
        if not s:
            continue
        if CHAPTER_LINE.match(s) and len(s) < 90:
            book.chapter(s)
            continue
        if len(s) < 60 and s.isupper() and len(s.split()) <= 8:
            book.add({"type": "heading", "level": 2, "text": esc(s)})
            continue
        book.add({"type": "para", "text": esc(s)})


# ---------------------------------------------------------------------------

def extract(path: Path, project: Path, opts: dict) -> dict:
    ext = path.suffix.lower()
    assets = project / "source" / "assets"
    fmt = {".pdf": "pdf", ".epub": "epub", ".html": "html", ".htm": "html", ".xhtml": "html",
           ".docx": "docx", ".md": "markdown", ".markdown": "markdown", ".txt": "text"}.get(ext)
    if fmt is None:
        raise SystemExit(f"unsupported input format: {ext} (use PDF, EPUB, HTML, DOCX, MD or TXT; "
                         "convert .doc/.mobi/.azw3 first, e.g. with Calibre's ebook-convert)")
    book = Book(path, fmt, assets)
    if fmt == "pdf":
        extract_pdf(path, book, opts)
    elif fmt == "epub":
        extract_epub(path, book, opts)
    elif fmt == "html":
        extract_html_text(path.read_text(encoding="utf-8", errors="replace"), book, path.parent,
                          opts.get("chapter_level"))
    elif fmt == "docx":
        extract_docx(path, book, opts)
    elif fmt == "markdown":
        extract_markdown(path, book, opts)
    else:
        extract_text(path, book, opts)
    data = book.to_json()
    if not data["chapters"]:
        raise SystemExit(f"no text found in {path.name}")
    return data
