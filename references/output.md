# Output: formats, typesetting, languages

`BT build --formats pdf,html,epub,docx,md` writes `output/<title>.<lang>.<ext>`
plus `output/assets/`.

| Format | Engine | What you get |
|---|---|---|
| html | built in | single page, cover, linked TOC, `lang`/`dir` set; images in `assets/` |
| pdf | Chromium (Edge/Chrome, or Playwright) prints the HTML | real shaping for every script, TOC with page numbers, PDF bookmarks (chapters + sections), page-number footer with Playwright, metadata |
| epub | built in (EPUB 3, also has toc.ncx) | one XHTML per chapter, nav TOC, `page-progression-direction="rtl"` for RTL languages |
| docx | python-docx | Word styles (Title, Heading 1-4, List Bullet/Number, Quote, Caption), TOC field that Word fills on open, tables, images, RTL and CJK settings |
| md | built in | one Markdown file with relative image links |

## Why the PDF goes through a browser

A browser lays out Arabic, Hebrew, Persian and Urdu letter-joining,
Devanagari and other Indic conjuncts, Thai, and CJK line breaking correctly,
and it applies the Unicode bidirectional algorithm. PDF libraries that draw
glyphs directly (reportlab, FPDF) need per-script workarounds and still get
Indic scripts wrong. The build is two passes: the first print finds the page
of every chapter and section through named destinations, and the second print
fills the TOC page numbers. PyMuPDF then adds bookmarks and metadata.

Page size: `BT build --page-size Letter` or `BT set page_size=A5`.
Browser: set `BOOK_BROWSER=/path/to/chrome` if it is not found automatically.

## Right-to-left languages (ar, he, fa, ur, ps, ku, ug, yi, dv)

- HTML/PDF/EPUB: `dir="rtl"` on the document; code blocks and inline code are
  isolated as left-to-right. A paragraph with no RTL letter, such as a version
  number or a kept English line, is laid out left-to-right so its digits and
  slashes are not reordered.
- DOCX: every paragraph gets `w:bidi` and logical `start` alignment. Tables get
  `w:bidiVisual`, so the first column is on the right. Sections get
  `w:bidi`, and `themeFontLang` is set. Mixed text is split into runs with
  `w:rtl` only on the RTL pieces, decided over the whole paragraph, so
  adjacent English words, links, `C#`, `.NET` and `DECIMAL(5,2)` keep their
  order and spaces.
- Never feed pre-shaped (reshaped/bidi-reordered) text into HTML or DOCX. Both
  engines shape the text themselves.

## CJK and other scripts

Font stacks per language are in `scripts/langs.py` (`LANGS`). The browser falls
back per glyph, so a missing font shows as a different face, not as boxes. If
boxes appear in the preview, install a Noto font for that script, or add a font
name to the stack. DOCX sets the East Asian font slot (YaHei, JhengHei,
Yu Gothic, Malgun Gothic) and the complex-script slot for Indic and Thai
scripts.

## Book metadata

- Title: translated as segment `meta.title`. Override with `BT set title=...`.
- `BT set author=...` and `BT set translator=...` (shown on the cover).
- The TOC heading is segment `meta.toc`.

## Adding a language

Add one line to `LANGS` in `scripts/langs.py`: `code: (name, [scripts], font stack)`.
For a new script, add its Unicode ranges to `SCRIPTS`, and to `RTL_SCRIPTS`
if it runs right to left. An unknown code still works as left-to-right, but
without the target-script check.
