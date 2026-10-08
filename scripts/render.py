"""Assemble the translated book: HTML, PDF, EPUB, DOCX, Markdown.

PDF is printed by a Chromium browser (Edge, Chrome or Playwright's Chromium),
because a browser shapes every script correctly - Arabic, Hebrew, Devanagari,
Thai, CJK - and applies the Unicode bidi algorithm, which reportlab-style
generators cannot do without per-script workarounds. The PDF is built twice:
the first pass finds the page of every chapter (via invisible markers), the
second prints the table of contents with real page numbers; the markers are
then removed and PDF bookmarks are added.
"""
from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import langs  # noqa: E402

# ---------------------------------------------------------------------------
# inline markup -> runs
# ---------------------------------------------------------------------------

# XML namespace identifiers required by the EPUB 3 / XHTML specs. They are
# names written into the output files, never URLs that are fetched.
NS_XHTML = "http://www.w3.org/1999/xhtml"
NS_OPS = "http://www.idpf.org/2007/ops"
NS_OPF = "http://www.idpf.org/2007/opf"
NS_NCX = "http://www.daisy.org/z3986/2005/ncx/"
NS_DC = "http://purl.org/dc/elements/1.1/"

LINK_AT = re.compile(r"\[((?:\\.|[^\]\\])*)\]\(([^)\s]+)\)")


def parse_inline(text: str, _disable: frozenset = frozenset()) -> list[dict]:
    """Split marked-up text into runs: {text, b, i, code, url}."""
    runs: list[dict] = []
    b = i = False
    buf: list[str] = []

    def emit(extra=None):
        if buf:
            runs.append({"text": "".join(buf), "b": b, "i": i, "code": False, "url": None})
            buf.clear()
        if extra:
            runs.append(extra)

    k, n = 0, len(text)
    while k < n:
        ch = text[k]
        if ch == "\\" and k + 1 < n and text[k + 1] in "*`[]\\":
            buf.append(text[k + 1])
            k += 2
            continue
        if ch == "`":
            end = text.find("`", k + 1)
            if end > k:
                emit({"text": text[k + 1:end], "b": b, "i": i, "code": True, "url": None})
                k = end + 1
                continue
        if text.startswith("**", k) and "b" not in _disable:
            emit()
            b = not b
            k += 2
            continue
        if ch == "*" and "i" not in _disable:
            nxt = text[k + 1] if k + 1 < n else " "
            prv = text[k - 1] if k > 0 else " "
            if (not i and not nxt.isspace()) or (i and not prv.isspace()):
                emit()
                i = not i
                k += 1
                continue
        if ch == "[":
            m = LINK_AT.match(text, k)
            if m:
                emit()
                for r in parse_inline(m.group(1)):
                    r["b"] = r["b"] or b
                    r["i"] = r["i"] or i
                    r["url"] = m.group(2)
                    runs.append(r)
                k = m.end()
                continue
        buf.append(ch)
        k += 1
    emit()
    if (b or i) and not _disable:
        # unbalanced marker: re-parse treating that marker as literal text
        return parse_inline(text, frozenset({"b"} if b else {"i"}) | (frozenset({"i"}) if i else frozenset()))
    return [r for r in runs if r["text"]]


def plain(text: str) -> str:
    return "".join(r["text"] for r in parse_inline(text))


def inline_html(text: str) -> str:
    out = []
    for r in parse_inline(text):
        t = html.escape(r["text"], quote=False)
        if r["code"]:
            t = f'<code dir="ltr">{t}</code>'
        if r["b"]:
            t = f"<strong>{t}</strong>"
        if r["i"]:
            t = f"<em>{t}</em>"
        if r["url"]:
            t = f'<a href="{html.escape(r["url"])}">{t}</a>'
        out.append(t)
    return "".join(out)


# ---------------------------------------------------------------------------
# translated model
# ---------------------------------------------------------------------------

def translated_book(p, tr: dict) -> dict:
    """Book structure with every translatable text replaced by its translation."""
    segs, notes = tr["segs"], tr["notes"]
    book = p.book
    missing = 0

    def T(sid: str, src: str) -> str:
        nonlocal missing
        if sid in segs:
            return src if segs[sid] is None else segs[sid]
        from book import needs_translation
        if needs_translation(src):
            missing += 1
        return src

    chapters = []
    for ch in book["chapters"]:
        cid = ch["id"]
        if cid in p.excluded():
            continue
        blocks = []
        for k, b in enumerate(ch["blocks"]):
            bid = f"{cid}.b{k:04d}"
            nb = dict(b)
            if b["type"] in ("heading", "para", "item", "quote", "caption"):
                nb["text"] = T(bid, b["text"])
            elif b["type"] == "table":
                nb["rows"] = [[T(f"{bid}.r{r}c{c}", cell) if cell.strip() else cell
                               for c, cell in enumerate(row)] for r, row in enumerate(b["rows"])]
            elif b["type"] == "figure" and b.get("alt"):
                nb["alt"] = T(f"{bid}.alt", b["alt"])
            blocks.append(nb)
            if bid in notes:
                blocks.append({"type": "note", "text": notes[bid]})
        chapters.append({"id": cid, "title": T(f"{cid}.t", ch["title"]) if ch["title"] else "",
                         "title_src": ch["title"], "blocks": blocks})
    meta = book["meta"]
    title = p.cfg.get("title") or T("meta.title", meta["title"])
    return {"title": title, "title_src": meta["title"], "author": p.cfg.get("author") or meta.get("author", ""),
            "translator": p.cfg.get("translator", ""), "toc": T("meta.toc", "Contents"),
            "chapters": chapters, "missing": missing}


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

CSS = """
@page { size: %(page)s; }
html { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
body { font-family: %(fonts)s; font-size: 11pt; line-height: 1.7; color: #1b1b1b;
       background: #fff; margin: 0; }
main { max-width: 46em; margin: 0 auto; padding: 0 16px; }
@media print { main { max-width: none; padding: 0; } }
p { margin: 0 0 0.6em; text-align: start; orphans: 2; widows: 2; }
h1, h2, h3, h4, h5 { line-height: 1.35; break-after: avoid; page-break-after: avoid; }
h1.chapter { font-size: 20pt; color: #0b3d66; border-bottom: 2px solid #0b3d66;
             padding-bottom: 6pt; margin: 0 0 14pt; break-before: page; page-break-before: always; }
h2 { font-size: 15pt; color: #7a3412; margin: 16pt 0 6pt; }
h3 { font-size: 13pt; color: #1f4e79; margin: 13pt 0 5pt; }
h4, h5 { font-size: 11.5pt; margin: 11pt 0 4pt; }
pre { font-family: %(mono)s; font-size: 8.8pt; line-height: 1.45; background: #f6f8fa;
      border: 1px solid #d0d7de; border-radius: 4px; padding: 7px 10px; margin: 4pt 0 9pt;
      white-space: pre-wrap; overflow-wrap: anywhere; direction: ltr; text-align: left; unicode-bidi: isolate; }
pre.short { break-inside: avoid; page-break-inside: avoid; }
code { font-family: %(mono)s; font-size: 0.9em; background: #f0f2f5; padding: 0 0.2em; border-radius: 3px; }
pre code { background: none; padding: 0; font-size: inherit; }
table { border-collapse: collapse; width: 100%%; margin: 6pt 0 10pt; font-size: 9.8pt; }
th, td { border: 1px solid #c9d1d9; padding: 3px 6px; text-align: start; vertical-align: top; }
th { background: #eef3f9; }
tr { break-inside: avoid; }
thead { display: table-header-group; }
figure { margin: 8pt 0; text-align: center; break-inside: avoid; page-break-inside: avoid; }
figure img { max-width: 100%%; max-height: 200mm; }
.caption { text-align: center; font-size: 9.5pt; color: #555; margin-top: -2pt; }
blockquote { margin: 6pt 0; padding-inline-start: 12px; border-inline-start: 3px solid #c9d1d9; color: #3d3d3d; }
ul, ol { margin: 0 0 0.6em; padding-inline-start: 1.6em; }
li { margin: 0.15em 0; }
hr { border: 0; border-top: 1px solid #ccc; margin: 12pt 25%%; }
.tnote { border: 1px solid #e0a800; background: #fff8e1; border-radius: 4px; padding: 5px 9px;
         font-size: 9.8pt; margin: 2pt 0 9pt; }
.tnote::before { content: "\\270E  "; }
.untranslated { background: #ffe9e9; }
.cover { min-height: 230mm; display: flex; flex-direction: column; justify-content: center;
         text-align: center; break-after: page; page-break-after: always; }
.cover h1 { font-size: 26pt; color: #0b3d66; margin: 0 0 10pt; }
.cover .orig { font-size: 13pt; color: #4a6782; unicode-bidi: plaintext; }
.cover .author { font-size: 13pt; margin-top: 22pt; }
.cover .translator { font-size: 11pt; color: #555; margin-top: 6pt; }
nav.toc { break-after: page; page-break-after: always; }
nav.toc h2 { font-size: 18pt; color: #0b3d66; }
nav.toc ol { list-style: none; padding: 0; margin: 0; }
nav.toc li { display: flex; gap: 8px; margin: 3pt 0; }
nav.toc li a { color: inherit; text-decoration: none; flex: 1; }
nav.toc li .pg { min-width: 2.5em; text-align: end; color: #555; }
a { color: #1f5f9e; }
a.self { color: inherit; text-decoration: none; }
"""


def _dir_attr(text: str, rtl_doc: bool) -> str:
    """In an RTL book, a block without any RTL letter (kept English, numbers, names)
    must be laid out left-to-right or its digits and slashes get reordered."""
    if rtl_doc and not langs.RTL_RE.search(plain(text)):
        return ' dir="ltr"'
    return ""


def blocks_html(blocks: list[dict], src_lang: str, mark_sections: bool, rtl_doc: bool = False) -> str:
    out: list[str] = []
    list_stack: list[str] = []

    def close_lists(depth=-1):
        while len(list_stack) > depth + 1:
            out.append(f"</li></{list_stack.pop()}>")

    for k, b in enumerate(blocks):
        t = b["type"]
        if t != "item":
            close_lists()
        if t == "heading":
            lvl = min(max(int(b.get("level", 2)), 1) + 1, 5)
            sid = f' id="{b["_sid"]}"' if b.get("_sid") else ""
            inner = inline_html(b["text"])
            if mark_sections and b.get("_top") and b.get("_sid") and "<a " not in inner:
                # a link to itself makes Chromium emit a named destination we can map to a page
                inner = f'<a class="self" href="#{b["_sid"]}">{inner}</a>'
            out.append(f"<h{lvl}{sid}{_dir_attr(b['text'], rtl_doc)}>{inner}</h{lvl}>")
        elif t == "para":
            out.append(f"<p{_dir_attr(b['text'], rtl_doc)}>{inline_html(b['text'])}</p>")
        elif t == "quote":
            out.append(f"<blockquote><p{_dir_attr(b['text'], rtl_doc)}>{inline_html(b['text'])}</p></blockquote>")
        elif t == "caption":
            out.append(f'<p class="caption"{_dir_attr(b["text"], rtl_doc)}>{inline_html(b["text"])}</p>')
        elif t == "note":
            out.append(f'<aside class="tnote">{inline_html(b["text"])}</aside>')
        elif t == "item":
            depth = int(b.get("depth", 0))
            tag = "ol" if b.get("ordered") else "ul"
            if len(list_stack) > depth + 1:
                close_lists(depth)
            if len(list_stack) == depth + 1:
                if list_stack[-1] != tag:
                    close_lists(depth - 1)
                    out.append(f"<{tag}><li>")
                    list_stack.append(tag)
                else:
                    out.append("</li><li>")
            while len(list_stack) < depth + 1:
                out.append(f"<{tag}><li>")
                list_stack.append(tag)
            d = _dir_attr(b["text"], rtl_doc)
            out.append(f"<span{d}>{inline_html(b['text'])}</span>" if d else inline_html(b["text"]))
        elif t == "code":
            lines = b["text"].count("\n") + 1
            cls = ' class="short"' if lines <= 30 else ""
            out.append(f'<pre dir="ltr"{cls}><code>{html.escape(b["text"])}</code></pre>')
        elif t == "table":
            rows = b["rows"]
            parts = ["<table>"]
            body = rows
            if b.get("header") and rows:
                parts.append("<thead><tr>" + "".join(f"<th>{inline_html(c)}</th>" for c in rows[0]) + "</tr></thead>")
                body = rows[1:]
            parts.append("<tbody>" + "".join(
                "<tr>" + "".join(f"<td>{inline_html(c)}</td>" for c in r) + "</tr>" for r in body) + "</tbody>")
            parts.append("</table>")
            out.append("".join(parts))
        elif t == "figure":
            alt = html.escape(plain(b.get("alt") or ""))
            out.append(f'<figure><img src="{html.escape(b["file"])}" alt="{alt}"></figure>')
        elif t == "rule":
            out.append("<hr>")
    close_lists()
    return "\n".join(out)


def book_html(tb: dict, src: dict, tgt: dict, page: str, pages: dict | None, markers: bool) -> str:
    css = CSS % {"fonts": tgt["fonts"], "mono": langs.MONO_STACK, "page": page}
    parts = [f'<!DOCTYPE html>\n<html lang="{tgt["code"]}" dir="{tgt["dir"]}">\n<head>\n<meta charset="utf-8">',
             f"<title>{html.escape(plain(tb['title']))}</title>",
             '<meta name="viewport" content="width=device-width, initial-scale=1">',
             f"<style>{css}</style>\n</head>\n<body>\n<main>"]
    cover = [f'<section class="cover"><h1>{inline_html(tb["title"])}</h1>']
    if plain(tb["title_src"]) != plain(tb["title"]):
        cover.append(f'<div class="orig" lang="{src["code"]}" dir="auto">{html.escape(plain(tb["title_src"]))}</div>')
    if tb["author"]:
        cover.append(f'<div class="author" dir="auto">{html.escape(tb["author"])}</div>')
    if tb["translator"]:
        cover.append(f'<div class="translator" dir="auto">{html.escape(tb["translator"])}</div>')
    cover.append("</section>")
    parts += cover
    toc = [f'<nav class="toc"><h2>{inline_html(tb["toc"])}</h2><ol>']
    for ch in tb["chapters"]:
        if not ch["title"]:
            continue
        pg = (pages or {}).get(ch["id"], "")
        toc.append(f'<li><a href="#{ch["id"]}">{inline_html(ch["title"])}</a>'
                   f'<span class="pg">{pg if pages is not None else "000"}</span></li>')
    toc.append("</ol></nav>")
    parts += toc
    for ch in tb["chapters"]:
        title = inline_html(ch["title"])
        if markers and "<a " not in title:
            title = f'<a class="self" href="#{ch["id"]}">{title or "&#8203;"}</a>'
        head = (f'<h1 class="chapter" id="{ch["id"]}">{title}</h1>' if ch["title"]
                else f'<h1 class="chapter" id="{ch["id"]}" style="border:0">{title if markers else ""}</h1>')
        blocks = []
        levels = [int(b.get("level", 2)) for b in ch["blocks"] if b["type"] == "heading"]
        top = min(levels) if levels else None
        for k, b in enumerate(ch["blocks"]):
            nb = dict(b)
            if b["type"] == "heading":
                nb["_sid"] = f"{ch['id']}-s{k}"
                nb["_top"] = int(b.get("level", 2)) == top
            blocks.append(nb)
        body = blocks_html(blocks, src["code"], markers, tgt["dir"] == "rtl")
        parts.append(f'<section class="chapter-body">{head}\n{body}</section>')
    parts.append("</main>\n</body>\n</html>\n")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# PDF via Chromium
# ---------------------------------------------------------------------------

BROWSER: str | None = None   # set from `build --browser PATH` / `doctor --browser PATH`


def _browser_candidates() -> list[str]:
    c = [BROWSER] if BROWSER else []
    if sys.platform.startswith("win"):
        for base in (r"C:\Program Files (x86)", r"C:\Program Files",
                     str(Path.home() / "AppData" / "Local")):
            c += [os.path.join(base, r"Microsoft\Edge\Application\msedge.exe"),
                  os.path.join(base, r"Google\Chrome\Application\chrome.exe"),
                  os.path.join(base, r"Chromium\Application\chrome.exe")]
    elif sys.platform == "darwin":
        c += ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
              "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
              "/Applications/Chromium.app/Contents/MacOS/Chromium"]
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
                 "microsoft-edge", "msedge", "chrome"):
        w = shutil.which(name)
        if w:
            c.append(w)
    return [x for x in c if x and os.path.exists(x)]


def html_to_pdf(html_path: Path, pdf_path: Path, page: str) -> str:
    """Print HTML to PDF. Returns the engine used."""
    footer = ('<div style="width:100%;font-size:8pt;color:#777;text-align:center;'
              'font-family:Arial,sans-serif"><span class="pageNumber"></span></div>')
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = None
            for opts in ({"channel": "msedge"}, {"channel": "chrome"}, {}):
                try:
                    browser = pw.chromium.launch(**opts)
                    break
                except Exception:
                    continue
            if browser is not None:
                pg = browser.new_page()
                pg.goto(html_path.resolve().as_uri(), wait_until="load")
                pg.pdf(path=str(pdf_path), format=page if page.upper() != "LETTER" else "Letter",
                       print_background=True, display_header_footer=True,
                       header_template="<span></span>", footer_template=footer,
                       margin={"top": "20mm", "bottom": "20mm", "left": "18mm", "right": "18mm"})
                browser.close()
                return "playwright"
    except ImportError:
        pass
    for exe in _browser_candidates():
        with tempfile.TemporaryDirectory() as prof:
            cmd = [exe, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
                   f"--user-data-dir={prof}", "--no-pdf-header-footer", "--print-to-pdf-no-header",
                   f"--print-to-pdf={pdf_path}", html_path.resolve().as_uri()]
            if not sys.platform.startswith("win"):
                cmd.insert(1, "--no-sandbox")   # containers/CI; only a local file is printed
            try:
                subprocess.run(cmd, check=True, timeout=600, capture_output=True)
            except Exception:
                continue
        if pdf_path.exists() and pdf_path.stat().st_size > 0:
            return f"cli:{Path(exe).name}"
    raise SystemExit("no Chromium browser found for PDF output. Install Microsoft Edge or Google Chrome, "
                     "or `pip install playwright && playwright install chromium`, or pass --browser with the path "
                     "to a Chrome/Edge executable. HTML/EPUB/DOCX/MD outputs do not need it.")


def _find_pages(pdf_path: Path) -> dict:
    """Map element ids (chapters, sections) to 0-based page numbers using the
    named destinations Chromium writes for in-document link targets."""
    import pymupdf
    found = {}
    doc = pymupdf.open(pdf_path)
    try:
        for name, dest in (doc.resolve_names() or {}).items():
            if isinstance(dest, dict) and dest.get("page", -1) >= 0:
                found[name] = (dest["page"], 0)
    except Exception:
        pass
    if not found:
        for page in doc:
            for l in page.get_links():
                if l.get("nameddest") and l.get("page", -1) >= 0:
                    found.setdefault(l["nameddest"], (l["page"], 0))
    doc.close()
    return found


def _finish_pdf(pdf_path: Path, tb: dict, marks: dict, tgt: dict) -> None:
    """Add bookmarks and metadata."""
    import pymupdf
    doc = pymupdf.open(pdf_path)
    toc = []
    for ch in tb["chapters"]:
        if ch["id"] in marks and ch["title"]:
            toc.append([1, plain(ch["title"])[:120], marks[ch["id"]][0] + 1])
            levels = [int(b.get("level", 2)) for b in ch["blocks"] if b["type"] == "heading"]
            top = min(levels) if levels else None
            for k, b in enumerate(ch["blocks"]):
                sid = f"{ch['id']}-s{k}"
                if b["type"] == "heading" and int(b.get("level", 2)) == top and sid in marks:
                    toc.append([2, plain(b["text"])[:120], marks[sid][0] + 1])
    if toc:
        doc.set_toc(toc)
    doc.set_metadata({"title": plain(tb["title"]), "author": tb.get("author", ""),
                      "subject": f"Translation into {tgt['name']}",
                      "creator": "book-translator", "producer": "book-translator"})
    tmp = pdf_path.with_suffix(".tmp.pdf")
    doc.save(tmp, garbage=3, deflate=True)
    doc.close()
    os.replace(tmp, pdf_path)


def build_pdf(out_dir: Path, stem: str, tb: dict, src: dict, tgt: dict, page: str) -> Path:
    html1 = out_dir / f".{stem}.pass1.html"
    pdf = out_dir / f"{stem}.pdf"
    html1.write_text(book_html(tb, src, tgt, page, None, markers=True), encoding="utf-8")
    engine = html_to_pdf(html1, pdf, page)
    marks = _find_pages(pdf)
    pages = {k: v[0] + 1 for k, v in marks.items()}
    html1.write_text(book_html(tb, src, tgt, page, pages, markers=True), encoding="utf-8")
    html_to_pdf(html1, pdf, page)
    marks = _find_pages(pdf)
    _finish_pdf(pdf, tb, marks, tgt)
    html1.unlink(missing_ok=True)
    if engine.startswith("cli"):
        print("note: printed with the browser CLI (no page-number footer). "
              "`pip install playwright` adds page numbers using the same browser.")
    return pdf


# ---------------------------------------------------------------------------
# EPUB 3
# ---------------------------------------------------------------------------

MEDIA = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
         ".svg": "image/svg+xml", ".webp": "image/webp", ".bmp": "image/bmp", ".tif": "image/tiff",
         ".tiff": "image/tiff", ".emf": "image/emf", ".wmf": "image/wmf"}


def _xhtml(fragment: str) -> str:
    fragment = re.sub(r"<(img|hr|br)([^>]*?)(?<!/)>", r"<\1\2/>", fragment)
    return fragment


def build_epub(out_dir: Path, stem: str, tb: dict, src: dict, tgt: dict, assets_dir: Path) -> Path:
    path = out_dir / f"{stem}.epub"
    uid = f"urn:uuid:{uuid.uuid4()}"
    css = (CSS % {"fonts": tgt["fonts"], "mono": langs.MONO_STACK, "page": "auto"}).replace("@page { size: auto; }", "")
    lang, d = tgt["code"], tgt["dir"]
    xhead = ('<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
             f'<html xmlns="{NS_XHTML}" xmlns:epub="{NS_OPS}" '
             f'lang="{lang}" xml:lang="{lang}" dir="{d}">\n')
    files, spine, nav_items = {}, [], []
    title_x = html.escape(plain(tb["title"]))
    files["OEBPS/cover.xhtml"] = (xhead + f"<head><title>{title_x}</title><link rel=\"stylesheet\" href=\"style.css\"/></head>"
                                  f"<body><section class=\"cover\"><h1>{inline_html(tb['title'])}</h1>"
                                  + (f"<div class=\"author\">{html.escape(tb['author'])}</div>" if tb["author"] else "")
                                  + "</section></body></html>")
    spine.append("cover")
    for n, ch in enumerate(tb["chapters"], 1):
        blocks = [dict(b, file=b["file"]) if b["type"] == "figure" else b for b in ch["blocks"]]
        body = blocks_html(blocks, src["code"], False, d == "rtl")
        head = f'<h1 class="chapter">{inline_html(ch["title"])}</h1>' if ch["title"] else ""
        name = f"ch{n:03d}.xhtml"
        ttl = html.escape(plain(ch["title"]) or title_x)
        files[f"OEBPS/{name}"] = (xhead + f"<head><title>{ttl}</title><link rel=\"stylesheet\" href=\"style.css\"/></head>"
                                  f"<body>{_xhtml(head + body)}</body></html>")
        spine.append(name[:-6])
        if ch["title"]:
            nav_items.append((name, plain(ch["title"])))
    nav = (xhead + f"<head><title>{html.escape(plain(tb['toc']))}</title></head><body>"
           f"<nav epub:type=\"toc\" id=\"toc\"><h1>{html.escape(plain(tb['toc']))}</h1><ol>"
           + "".join(f'<li><a href="{f}">{html.escape(t)}</a></li>' for f, t in nav_items)
           + "</ol></nav></body></html>")
    files["OEBPS/nav.xhtml"] = nav
    ncx = ('<?xml version="1.0" encoding="utf-8"?>\n'
           f'<ncx xmlns="{NS_NCX}" version="2005-1">'
           f'<head><meta name="dtb:uid" content="{uid}"/></head><docTitle><text>{title_x}</text></docTitle><navMap>'
           + "".join(f'<navPoint id="n{i}" playOrder="{i}"><navLabel><text>{html.escape(t)}</text></navLabel>'
                     f'<content src="{f}"/></navPoint>' for i, (f, t) in enumerate(nav_items, 1))
           + "</navMap></ncx>")
    files["OEBPS/toc.ncx"] = ncx
    files["OEBPS/style.css"] = css
    manifest = ['<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
                '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
                '<item id="css" href="style.css" media-type="text/css"/>',
                '<item id="cover" href="cover.xhtml" media-type="application/xhtml+xml"/>']
    for s in spine[1:]:
        manifest.append(f'<item id="{s}" href="{s}.xhtml" media-type="application/xhtml+xml"/>')
    used = sorted({b["file"] for ch in tb["chapters"] for b in ch["blocks"] if b["type"] == "figure"})
    for k, f in enumerate(used):
        ext = Path(f).suffix.lower()
        manifest.append(f'<item id="img{k}" href="{f}" media-type="{MEDIA.get(ext, "image/png")}"/>')
    import time as _t
    opf = ('<?xml version="1.0" encoding="utf-8"?>\n'
           f'<package xmlns="{NS_OPF}" version="3.0" unique-identifier="bookid" '
           f'xml:lang="{lang}" dir="{d}">\n<metadata xmlns:dc="{NS_DC}">'
           f'<dc:identifier id="bookid">{uid}</dc:identifier><dc:title>{title_x}</dc:title>'
           f'<dc:language>{lang}</dc:language>'
           + (f"<dc:creator>{html.escape(tb['author'])}</dc:creator>" if tb["author"] else "")
           + f'<meta property="dcterms:modified">{_t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime())}</meta>'
           '</metadata>\n<manifest>' + "".join(manifest) + '</manifest>\n'
           f'<spine toc="ncx"' + (' page-progression-direction="rtl"' if d == "rtl" else "") + ">"
           + "".join(f'<itemref idref="{s}"/>' for s in spine) + "</spine>\n</package>")
    files["OEBPS/content.opf"] = opf
    container = ('<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                 '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
                 '</rootfiles></container>')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", container, compress_type=zipfile.ZIP_DEFLATED)
        for name, data in files.items():
            z.writestr(name, data, compress_type=zipfile.ZIP_DEFLATED)
        for f in used:
            src_file = assets_dir.parent / f
            if src_file.exists():
                z.write(src_file, f"OEBPS/{f}", compress_type=zipfile.ZIP_DEFLATED)
    return path


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------

def build_md(out_dir: Path, stem: str, tb: dict) -> Path:
    lines = [f"# {tb['title']}", ""]
    if tb["author"]:
        lines += [tb["author"], ""]
    for ch in tb["chapters"]:
        if ch["title"]:
            lines += [f"## {ch['title']}", ""]
        for b in ch["blocks"]:
            t = b["type"]
            if t == "heading":
                lines += ["#" * min(int(b.get("level", 2)) + 2, 6) + " " + b["text"], ""]
            elif t in ("para", "caption"):
                lines += [b["text"], ""]
            elif t == "quote":
                lines += ["> " + b["text"], ""]
            elif t == "note":
                lines += ["> \u270E " + b["text"], ""]
            elif t == "item":
                ind = "  " * int(b.get("depth", 0))
                lines += [f"{ind}{'1.' if b.get('ordered') else '-'} {b['text']}"]
            elif t == "code":
                fence = "````" if "```" in b["text"] else "```"
                lines += [fence, b["text"], fence, ""]
            elif t == "table" and b["rows"]:
                w = max(len(r) for r in b["rows"])
                rows = [[c.replace("|", "\\|") for c in r] + [""] * (w - len(r)) for r in b["rows"]]
                lines += ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * w]
                lines += ["| " + " | ".join(r) + " |" for r in rows[1:]] + [""]
            elif t == "figure":
                lines += [f"![{plain(b.get('alt') or '')}]({b['file']})", ""]
            elif t == "rule":
                lines += ["---", ""]
        lines.append("")
    # list items need a blank line after the list
    text = re.sub(r"(\n(?:\s*(?:-|1\.) [^\n]*))\n(?=[^\s\-1])", r"\1\n\n", "\n".join(lines))
    path = out_dir / f"{stem}.md"
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# DOCX
# ---------------------------------------------------------------------------

LOCALES = {"ar": "ar-SA", "he": "he-IL", "fa": "fa-IR", "ur": "ur-PK", "ps": "ps-AF", "ku": "ku-Arab-IQ",
           "yi": "yi-001", "ug": "ug-CN", "dv": "dv-MV", "zh": "zh-CN", "zh-tw": "zh-TW", "ja": "ja-JP",
           "ko": "ko-KR", "en": "en-US", "fr": "fr-FR", "es": "es-ES", "de": "de-DE", "pt": "pt-BR",
           "it": "it-IT", "ru": "ru-RU", "tr": "tr-TR", "hi": "hi-IN", "th": "th-TH", "el": "el-GR"}
CS_FONT = {"arabic": "Arial", "hebrew": "Arial", "devanagari": "Nirmala UI", "bengali": "Nirmala UI",
           "tamil": "Nirmala UI", "telugu": "Nirmala UI", "kannada": "Nirmala UI", "malayalam": "Nirmala UI",
           "gujarati": "Nirmala UI", "gurmukhi": "Nirmala UI", "oriya": "Nirmala UI", "sinhala": "Nirmala UI",
           "thai": "Leelawadee UI", "lao": "Leelawadee UI", "khmer": "Leelawadee UI", "thaana": "MV Boli",
           "syriac": "Estrangelo Edessa", "myanmar": "Myanmar Text", "ethiopic": "Ebrima", "tibetan": "Microsoft Himalaya"}
EA_FONT = {"zh": "Microsoft YaHei", "zh-tw": "Microsoft JhengHei", "ja": "Yu Gothic", "ko": "Malgun Gothic"}
AFFIX_BEFORE = set("+-.#@$/~\\")
AFFIX_AFTER = set("+#")
BRACKETS = {"(": ")", "[": "]", "{": "}", "<": ">", "\u00ab": "\u00bb", "\u2039": "\u203a"}
CLOSERS = {v: k for k, v in BRACKETS.items()}
LRE, PDF_ = "\u202a", "\u202c"

# OOXML property containers are xsd:sequence: children must be in schema order
# (ECMA-376 Part 1, section 17). Word repairs some misordering, other readers do not.
_ORDER = {
    "rPr": ["rStyle", "rFonts", "b", "bCs", "i", "iCs", "caps", "smallCaps", "strike", "dstrike",
            "outline", "shadow", "emboss", "imprint", "noProof", "snapToGrid", "vanish", "webHidden",
            "color", "spacing", "w", "kern", "position", "sz", "szCs", "highlight", "u", "effect",
            "bdr", "shd", "fitText", "vertAlign", "rtl", "cs", "em", "lang", "eastAsianLayout",
            "specVanish", "oMath"],
    "pPr": ["pStyle", "keepNext", "keepLines", "pageBreakBefore", "framePr", "widowControl", "numPr",
            "suppressLineNumbers", "pBdr", "shd", "tabs", "suppressAutoHyphens", "kinsoku", "wordWrap",
            "overflowPunct", "topLinePunct", "autoSpaceDE", "autoSpaceDN", "bidi", "adjustRightInd",
            "snapToGrid", "spacing", "ind", "contextualSpacing", "mirrorIndents", "suppressOverlap", "jc",
            "textDirection", "textAlignment", "textboxTightWrap", "outlineLvl", "divId", "cnfStyle",
            "rPr", "sectPr", "pPrChange"],
    "tblPr": ["tblStyle", "tblpPr", "tblOverlap", "bidiVisual", "tblStyleRowBandSize",
              "tblStyleColBandSize", "tblW", "jc", "tblCellSpacing", "tblInd", "tblBorders", "shd",
              "tblLayout", "tblCellMar", "tblLook", "tblCaption", "tblDescription"],
    "trPr": ["cnfStyle", "divId", "gridBefore", "gridAfter", "wBefore", "wAfter", "cantSplit",
             "trHeight", "tblHeader", "tblCellSpacing", "jc", "hidden"],
    "sectPr": ["headerReference", "footerReference", "footnotePr", "endnotePr", "type", "pgSz", "pgMar",
               "paperSrc", "pgBorders", "lnNumType", "pgNumType", "cols", "formProt", "vAlign",
               "noEndnote", "titlePg", "textDirection", "bidi", "rtlGutter", "docGrid", "printerSettings",
               "sectPrChange"],
}


def split_rtl(text: str) -> list[tuple[str, bool]]:
    """Split mixed text into (chunk, is_rtl) runs for Word.

    Ported from the arabic-docx skill (same author, MIT) and generalised to every
    right-to-left script. Neutrals join the RTL side unless they sit between two
    LTR tokens; flush affixes (C#, .NET, +963) stay with their token; a bracket
    pair is never split across runs.
    """
    if not text:
        return []
    kinds = []
    for ch in text:
        if langs.RTL_RE.match(ch):
            kinds.append("A")
        elif ch.isalnum():
            kinds.append("L")
        else:
            kinds.append("N")
    n = len(kinds)
    prev_s, last = [None] * n, None
    for i, k in enumerate(kinds):
        prev_s[i] = last
        if k != "N":
            last = k
    next_s, nxt = [None] * n, None
    for i in range(n - 1, -1, -1):
        next_s[i] = nxt
        if kinds[i] != "N":
            nxt = kinds[i]
    res = [k if k != "N" else ("L" if prev_s[i] == "L" and next_s[i] == "L" else "A")
           for i, k in enumerate(kinds)]
    for i in range(n):
        if kinds[i] != "L":
            continue
        j = i - 1
        while j >= 0 and kinds[j] == "N" and text[j] in AFFIX_BEFORE:
            j -= 1
        if j < i - 1 and (j < 0 or text[j].isspace() or kinds[j] == "L" or text[j] in BRACKETS):
            res[j + 1:i] = ["L"] * (i - 1 - j)
        k = i + 1
        while k < n and kinds[k] == "N" and text[k] in AFFIX_AFTER:
            k += 1
        if k > i + 1:
            res[i + 1:k] = ["L"] * (k - i - 1)
    stack = []
    for i, ch in enumerate(text):
        if ch in BRACKETS:
            stack.append((ch, i))
        elif ch in CLOSERS:
            for q in range(len(stack) - 1, -1, -1):
                if stack[q][0] == CLOSERS[ch]:
                    o = stack[q][1]
                    del stack[q:]
                    if (res[o] == "L" or res[i] == "L") and "A" not in kinds[o:i + 1]:
                        res[o:i + 1] = ["L"] * (i - o + 1)
                    break
    out, buf, cur = [], text[0], res[0]
    for ch, k in zip(text[1:], res[1:]):
        if k == cur:
            buf += ch
        else:
            out.append((buf, cur == "A"))
            buf, cur = ch, k
    out.append((buf, cur == "A"))
    return [(t, a) for t, a in out if t]


def build_docx(out_dir: Path, stem: str, tb: dict, tgt: dict, assets_dir: Path) -> Path:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.opc.constants import RELATIONSHIP_TYPE as RT
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Cm, Pt, RGBColor

    rtl = tgt["dir"] == "rtl"
    code = tgt["code"]
    locale = LOCALES.get(code, code)
    cs_font = next((CS_FONT[s] for s in tgt["scripts"] if s in CS_FONT), "Arial")
    ea_font = EA_FONT.get(code)
    latin_font = "Calibri"
    doc = Document()

    def sub(parent, tag):
        """Child `tag` of parent, created at its ECMA-376 schema position."""
        el = parent.find(qn(tag))
        if el is not None:
            return el
        el = OxmlElement(tag)
        order = _ORDER.get(parent.tag.rsplit("}", 1)[-1])
        name = tag.split(":", 1)[1]
        if order and name in order:
            later = set(order[order.index(name) + 1:])
            for sib in parent:
                if sib.tag.rsplit("}", 1)[-1] in later:
                    sib.addprevious(el)
                    return el
        parent.append(el)
        return el

    def set_fonts(rpr, mono=False):
        f = sub(rpr, "w:rFonts")
        for a in ("w:asciiTheme", "w:hAnsiTheme", "w:eastAsiaTheme", "w:cstheme"):
            f.attrib.pop(qn(a), None)
        lf = "Consolas" if mono else latin_font
        f.set(qn("w:ascii"), lf)
        f.set(qn("w:hAnsi"), lf)
        f.set(qn("w:cs"), "Consolas" if mono else cs_font)
        if ea_font:
            f.set(qn("w:eastAsia"), ea_font)

    # document defaults: fonts, languages, direction
    styles = doc.styles.element
    rpr_def = styles.find(qn("w:docDefaults")).find(qn("w:rPrDefault")).find(qn("w:rPr"))
    set_fonts(rpr_def)
    lang = sub(rpr_def, "w:lang")
    lang.set(qn("w:val"), locale if not rtl else "en-US")
    if rtl:
        lang.set(qn("w:bidi"), locale)
        settings = doc.settings.element
        tfl = settings.find(qn("w:themeFontLang"))
        if tfl is None:
            tfl = OxmlElement("w:themeFontLang")
            settings.append(tfl)
        tfl.set(qn("w:bidi"), locale)
        ppr_def = styles.find(qn("w:docDefaults")).find(qn("w:pPrDefault"))
        if ppr_def is None:
            ppr_def = OxmlElement("w:pPrDefault")
            styles.find(qn("w:docDefaults")).append(ppr_def)
        ppr = sub(ppr_def, "w:pPr")
        sub(ppr, "w:bidi")
        sub(ppr, "w:jc").set(qn("w:val"), "start")
    if ea_font:
        lang.set(qn("w:eastAsia"), locale)
    # strip theme fonts and LTR overrides from styles so headings use the right faces
    for rpr in styles.iter(qn("w:rPr")):
        f = rpr.find(qn("w:rFonts"))
        if f is not None and any(f.get(qn(a)) for a in ("w:asciiTheme", "w:hAnsiTheme", "w:cstheme", "w:eastAsiaTheme")):
            set_fonts(rpr)
        b, sz = rpr.find(qn("w:b")), rpr.find(qn("w:sz"))
        if b is not None and rpr.find(qn("w:bCs")) is None:
            sub(rpr, "w:bCs")
        if sz is not None and rpr.find(qn("w:szCs")) is None:
            sub(rpr, "w:szCs").set(qn("w:val"), sz.get(qn("w:val")))
    doc.styles["Normal"].font.size = Pt(11)
    sec = doc.sections[0]
    sec.left_margin = sec.right_margin = Cm(2.2)
    if rtl:
        sub(sec._sectPr, "w:bidi")
    # code style
    from docx.enum.style import WD_STYLE_TYPE
    cs = doc.styles.add_style("Code Block", WD_STYLE_TYPE.PARAGRAPH)
    cs.base_style = doc.styles["Normal"]
    cs.font.size = Pt(8.5)
    cs.paragraph_format.space_after = Pt(0)
    cs.paragraph_format.space_before = Pt(0)
    set_fonts(cs.element.get_or_add_rPr(), mono=True)
    shd = sub(cs.element.get_or_add_pPr(), "w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), "F3F5F7")

    def para_dir(p, align="start", ltr=False):
        ppr = p._p.get_or_add_pPr()
        if rtl and not ltr:
            sub(ppr, "w:bidi")
        elif rtl and ltr:
            sub(ppr, "w:bidi").set(qn("w:val"), "0")
        jc = sub(ppr, "w:jc")
        jc.set(qn("w:val"), align if not ltr else "left")

    def pieces_of(runs):
        """Direction is resolved over the whole paragraph, then cut per run, so a
        space between two English links (separate runs) stays with them."""
        if not rtl:
            return [[(r["text"], False)] for r in runs]
        full = "".join(r["text"] if not r["code"] else "a" * len(r["text"]) for r in runs)
        flags = []
        for chunk, is_rtl in split_rtl(full):
            flags.extend([is_rtl] * len(chunk))
        out, k = [], 0
        for r in runs:
            n = len(r["text"])
            f = flags[k:k + n]
            k += n
            if r["code"]:
                out.append([(r["text"], False)])
                continue
            parts, start = [], 0
            for j in range(1, n + 1):
                if j == n or f[j] != f[start]:
                    parts.append((r["text"][start:j], f[start]))
                    start = j
            out.append(parts)
        return out

    def add_text(p, text, bold=False, italic=False, size=None, color=None):
        runs = parse_inline(text)
        for r, pieces in zip(runs, pieces_of(runs)):
            for chunk, is_rtl in pieces:
                if not is_rtl and rtl and not r["code"] and chunk and \
                        (chunk[0] in AFFIX_BEFORE or chunk[-1] in AFFIX_AFTER):
                    chunk = LRE + chunk + PDF_
                if r["url"]:
                    rid = p.part.relate_to(r["url"], RT.HYPERLINK, is_external=True)
                    hl = OxmlElement("w:hyperlink")
                    hl.set(qn("r:id"), rid)
                    run = OxmlElement("w:r")
                    hl.append(run)
                    p._p.append(hl)
                    from docx.text.run import Run
                    run_obj = Run(run, p)
                    run_obj.text = chunk
                    run_obj.font.color.rgb = RGBColor(0x1F, 0x5F, 0x9E)
                    run_obj.font.underline = True
                else:
                    run_obj = p.add_run(chunk)
                rpr = run_obj._r.get_or_add_rPr()
                set_fonts(rpr, mono=r["code"])
                if bold or r["b"]:
                    sub(rpr, "w:b")
                    sub(rpr, "w:bCs")
                if italic or r["i"]:
                    sub(rpr, "w:i")
                    sub(rpr, "w:iCs")
                if size:
                    half = str(int(size * 2))
                    sub(rpr, "w:sz").set(qn("w:val"), half)
                    sub(rpr, "w:szCs").set(qn("w:val"), half)
                if color:
                    run_obj.font.color.rgb = color
                if is_rtl:
                    sub(rpr, "w:rtl")
                    ln = sub(rpr, "w:lang")
                    ln.set(qn("w:bidi"), locale)
                elif rtl:
                    sub(rpr, "w:lang").set(qn("w:val"), "en-US")
                # schema order inside rPr: rFonts first is enough for Word; keep rtl/lang last
        return p

    def heading(text, level, page_break=False):
        p = doc.add_paragraph(style=f"Heading {min(level, 4)}")
        para_dir(p)
        if page_break:
            sub(p._p.get_or_add_pPr(), "w:pageBreakBefore")
        add_text(p, text)
        return p

    # cover
    p = doc.add_paragraph(style="Title")
    para_dir(p, "center")
    add_text(p, tb["title"])
    if plain(tb["title_src"]) != plain(tb["title"]):
        p = doc.add_paragraph()
        para_dir(p, "center", ltr=not langs.RTL_RE.search(tb["title_src"] or ""))
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        add_text(p, tb["title_src"], size=13, color=RGBColor(0x4A, 0x67, 0x82))
    if tb["author"]:
        p = doc.add_paragraph()
        para_dir(p, "center")
        add_text(p, tb["author"], size=13)
    # TOC field (Word fills it when the document is opened)
    p = doc.add_paragraph()      # not a Heading style, so it is not listed in its own TOC
    para_dir(p)
    sub(p._p.get_or_add_pPr(), "w:pageBreakBefore")
    add_text(p, tb["toc"], bold=True, size=18, color=RGBColor(0x0B, 0x3D, 0x66))
    p = doc.add_paragraph()
    para_dir(p)
    for kind, val in (("begin", None), ("instr", ' TOC \\o "1-2" \\h \\z \\u '), ("separate", None), ("text", "..."), ("end", None)):
        r = OxmlElement("w:r")
        if kind == "instr":
            it = OxmlElement("w:instrText")
            it.set(qn("xml:space"), "preserve")
            it.text = val
            r.append(it)
        elif kind == "text":
            t = OxmlElement("w:t")
            t.text = val
            r.append(t)
        else:
            fc = OxmlElement("w:fldChar")
            fc.set(qn("w:fldCharType"), kind)
            r.append(fc)
        p._p.append(r)
    upd = OxmlElement("w:updateFields")
    upd.set(qn("w:val"), "true")
    doc.settings.element.append(upd)

    for ch in tb["chapters"]:
        heading(ch["title"] or " ", 1, page_break=True)
        for b in ch["blocks"]:
            t = b["type"]
            if t == "heading":
                heading(b["text"], min(int(b.get("level", 2)) + 1, 4))
            elif t in ("para", "quote", "caption", "note"):
                style = {"quote": "Quote", "caption": "Caption"}.get(t)
                p = doc.add_paragraph(style=style) if style else doc.add_paragraph()
                para_dir(p, "center" if t == "caption" else "start")
                if t == "note":
                    sh = sub(p._p.get_or_add_pPr(), "w:shd")
                    sh.set(qn("w:val"), "clear")
                    sh.set(qn("w:fill"), "FFF8E1")
                    add_text(p, "\u270E " + b["text"], size=10)
                else:
                    add_text(p, b["text"])
            elif t == "item":
                depth = int(b.get("depth", 0))
                base = "List Number" if b.get("ordered") else "List Bullet"
                style = base if depth == 0 else f"{base} {min(depth + 1, 3)}"
                try:
                    p = doc.add_paragraph(style=style)
                except KeyError:
                    p = doc.add_paragraph(style=base)
                para_dir(p)
                add_text(p, b["text"])
            elif t == "code":
                for line in b["text"].split("\n"):
                    p = doc.add_paragraph(style="Code Block")
                    para_dir(p, ltr=True)
                    r = p.add_run(line if line else " ")
                    set_fonts(r._r.get_or_add_rPr(), mono=True)
                doc.add_paragraph().paragraph_format.space_after = Pt(2)
            elif t == "table" and b["rows"]:
                w = max(len(r) for r in b["rows"])
                table = doc.add_table(rows=0, cols=w)
                table.style = "Table Grid"
                if rtl:
                    sub(table._tbl.tblPr, "w:bidiVisual")
                for ri, row in enumerate(b["rows"]):
                    cells = table.add_row().cells
                    if ri == 0 and b.get("header"):
                        sub(table.rows[-1]._tr.get_or_add_trPr(), "w:tblHeader")
                    for ci in range(w):
                        cp = cells[ci].paragraphs[0]
                        para_dir(cp)
                        add_text(cp, row[ci] if ci < len(row) else "", bold=(ri == 0 and b.get("header")), size=10)
                doc.add_paragraph()
            elif t == "figure":
                f = assets_dir.parent / b["file"]
                if f.exists():
                    try:
                        from PIL import Image
                        with Image.open(f) as im:
                            wpx = im.size[0]
                        width = Cm(min(15.5, wpx / 200 * 2.54 * 1.4))
                        doc.add_picture(str(f), width=width)
                        doc.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER
                    except Exception:
                        pass
            elif t == "rule":
                p = doc.add_paragraph("* * *")
                p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    core = doc.core_properties
    core.title = plain(tb["title"])
    core.author = tb.get("author", "")
    core.language = locale
    path = out_dir / f"{stem}.docx"
    doc.save(path)
    return path


# ---------------------------------------------------------------------------

def build(p, tr: dict, formats: list[str], page_size: str = "A4") -> list[Path]:
    tb = translated_book(p, tr)
    src, tgt = p.src, p.tgt
    out_dir = p.root / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    assets_src = p.root / "source" / "assets"
    assets_out = out_dir / "assets"
    used = {b["file"] for ch in tb["chapters"] for b in ch["blocks"] if b["type"] == "figure"}
    if used:
        assets_out.mkdir(exist_ok=True)
        for f in used:
            s = p.root / "source" / f
            if s.exists():
                shutil.copy2(s, out_dir / f)
    slug = re.sub(r"[^\w]+", "-", plain(tb["title_src"]), flags=re.U).strip("-")[:60] or "book"
    stem = f"{slug}.{tgt['code']}"
    outs = []
    unknown = [f for f in formats if f not in ("html", "pdf", "epub", "docx", "md")]
    if unknown:
        raise SystemExit(f"unknown format(s): {', '.join(unknown)}")
    if "html" in formats:
        h = out_dir / f"{stem}.html"
        h.write_text(book_html(tb, src, tgt, page_size, {}, markers=False), encoding="utf-8")
        outs.append(h)
    if "pdf" in formats:
        outs.append(build_pdf(out_dir, stem, tb, src, tgt, page_size))
    if "epub" in formats:
        outs.append(build_epub(out_dir, stem, tb, src, tgt, assets_out))
    if "docx" in formats:
        outs.append(build_docx(out_dir, stem, tb, tgt, assets_out))
    if "md" in formats:
        outs.append(build_md(out_dir, stem, tb))
    if tb["missing"]:
        print(f"DRAFT: {tb['missing']} segments are untranslated and use the source text")
    return outs
