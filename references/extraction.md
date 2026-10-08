# Extraction: inputs, options, fixing problems

`init` converts the source into `source/book.json`: chapters, each a list of
blocks (`heading`, `para`, `item`, `quote`, `caption`, `code`, `table`,
`figure`, `rule`). Only text blocks, table cells, chapter titles and image alt
text become translation segments. Code and images are copied as they are.

Inline formatting survives as `**bold**`, `*italic*`, `` `code` `` and
`[text](url)`. Literal `*`, `` ` ``, `[` and `]` in the source are escaped
with a backslash.

## Formats

| Input | How it is read | Notes |
|---|---|---|
| PDF | PyMuPDF, text layer only | see below |
| EPUB | spine order, XHTML parsed | DRM-protected EPUBs are refused; `nav` document skipped |
| HTML | one file; chapters split at `h1` (or `h2` if there is a single `h1`) | local images copied, data: URIs decoded |
| DOCX | python-docx, body order | Heading styles become chapters and headings; lists, tables, inline images, monospace runs and hyperlinks kept |
| Markdown | CommonMark subset | front matter `title:`/`author:`/`lang:`; fenced and indented code, tables, nested lists, images |
| TXT | blank-line paragraphs | lines like "Chapter 3", "CAPÍTULO", "Глава", "الفصل", "第三章" start chapters |

Other formats: convert first. Calibre's `ebook-convert book.mobi book.epub`
handles MOBI and AZW3; LibreOffice's `soffice --headless --convert-to docx file.doc`
handles DOC and ODT.

## PDF specifics

- **Chapters** come from the PDF outline (bookmarks), at the first level with
  2+ entries (`--chapter-level N` to override). Without an outline, chapters
  split at the largest heading size.
- **Headings** are lines in a font clearly larger than the body text, used on
  several pages. Levels follow size order. Short all-bold lines become minor
  headings.
- **Code** is any line whose font is monospaced (font flags or names like Mono,
  Courier, Consolas). Indentation is rebuilt from x positions.
- **Page furniture**: lines in the top or bottom 8.5% of the page that repeat
  on 20% or more of pages (running heads, footers, page numbers) are dropped.
- **Paragraphs** are rebuilt from lines using vertical gaps and first-line
  indents. They continue across page breaks when the sentence is unfinished.
  Soft hyphens are rejoined (`transla-` + `tion`).
- **Tables**: ruled tables are found with PyMuPDF `find_tables`. Borderless
  tables (aligned columns, e.g. "command | description") are found from line
  positions: three or more rows with the same column starts and a short first
  cell. Wrapped cell text stays in its cell, even when the first cell is
  vertically centred. A grid that is mostly empty, or that has running text
  passing through it (boxes drawn behind inline code), is not a table. If the
  first column holds only "1." or "-" markers, it becomes a list.
- **Lists**: a line starting with a bullet character, a number ("1.", "a)"),
  or a small drawn dot to its left starts a list item.
- **Figures** are raster images, plus vector drawings that are not text boxes.
  A drawn region is skipped if it is a code box or callout (text fills it, or
  the text inside is code or full sentences), a page background (over half
  the page), a decoration bleeding off the page edge, or a highlight behind
  running text. In those cases the text is extracted instead. Each figure is rendered to
  PNG at 200 dpi. Text inside a figure is listed as `labels` and stays
  untranslated in the image.
- **Scanned PDFs** (no text layer) are refused with an OCR hint:
  `ocrmypdf --language eng in.pdf out.pdf`.

| Symptom (see `BT show`) | Fix |
|---|---|
| Cover, TOC or ad pages became chapters | `BT exclude <ids>` |
| Text from two columns interleaved | `init --force --columns 2` |
| Chapters too fine or too coarse | `init --force --chapter-level 1` / `2` |
| A code box was read as a table | `init --force --no-tables` |
| Junk pages at the start or end | `init --force --skip-pages 1-6,300-310` |
| Running header text inside paragraphs | `--skip-pages` the worst pages, or accept it; the translator translates what it gets |
| Report says "undecodable glyphs" | The PDF font has no text mapping for some ligatures; check those words with `show`, and tell the translator in `style` (e.g. "o?cial = official") |

`init --force` re-extracts and re-plans. Use it **only before translation
starts**, because segment ids can change with the extraction.

## Language detection

The source language comes from the file's own metadata if present (EPUB
`dc:language`, HTML `lang`, DOCX core properties, Markdown front matter).
Otherwise it is detected from the script and frequent words. Override with
`--from xx`.
