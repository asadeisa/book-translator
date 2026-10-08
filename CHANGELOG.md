# Changelog

## Unreleased (branch `voice`)

- Voice mode for literary books: `voice study` (author research, style
  profile, brief, people), the brief and the chunk's people in every task,
  and `edit` (a voice-edit pass with drift warnings against the draft).
- `show --full` prints whole paragraphs.

## 1.0.1 — 2026-10-08

Changes for the Agensi security scan. Behaviour is the same.

- No environment variables are read. `BOOK_BROWSER` is replaced by
  `build --browser PATH` (and `doctor --browser PATH`); `BOOK_PROJECT` is
  removed, so use `--project`.
- The fixed URI strings (the link pattern and the EPUB/XHTML namespace
  identifiers) moved to `scripts/uris.py`. The DOCX writer uses
  `qn("xml:space")`.
- SKILL.md has a "Security and permissions" section.

## 1.0.0 — 2026-10-08

First release.

- Extraction from PDF (text layer), EPUB, DOCX, HTML, Markdown and TXT into
  chapters and blocks. Tables with and without borders, drawn bullets, code
  boxes, page furniture and figure detection are handled.
- Chunk plan, self-contained task files, and a plain-text `@@ id`
  translation format.
- Checks for missing or misplaced segments, lost code, URLs and keep-terms,
  untranslated text, dropped sentences, missing numbers, length outliers and
  the glossary.
- A glossary with sense-limited terms and translator proposals.
- Parallel translation with claimed chunks, `status`, and side-by-side review
  sheets.
- `init --force` carries finished translations over to a re-extracted book.
- Output to PDF (through Chromium, with a TOC with page numbers and bookmarks),
  EPUB 3, DOCX (RTL- and CJK-aware), HTML and Markdown.
