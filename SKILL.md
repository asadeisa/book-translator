---
name: book-translator
description: >-
  Translate a whole book or any long document (PDF, EPUB, DOCX, HTML, Markdown,
  TXT) from any language into any language without losing a sentence: extracts
  the structure, keeps code, tables and figures, splits the text into
  context-safe chunks, enforces one glossary, verifies every chunk
  automatically, lets subagents translate in parallel, and rebuilds the book as
  PDF, EPUB, DOCX, HTML or Markdown with correct right-to-left and CJK
  typesetting. Use whenever the user wants a book, manual, course, thesis,
  report or documentation translated that is too long to translate in one
  pass, or asks to resume or check such a translation.
---

# Book Translator

A whole book does not fit in one context window, and translating it "from
memory" in big pieces is how sentences, numbers, links and code get lost. This
skill turns the book into numbered segments, gives the translator one small,
self-contained task at a time, checks every result mechanically against the
source, and only then rebuilds the book. All state lives on disk, so the work
survives context resets and can be split across parallel agents.

Scripts: `scripts/book.py` (one CLI, run `python scripts/book.py -h`).
In the commands below `BT` means `python <skill-dir>/scripts/book.py`.

## Setup

```
pip install pymupdf python-docx pillow
BT doctor
```
PDF output prints through Microsoft Edge or Google Chrome (found automatically;
`pip install playwright` adds page numbers in the footer, using the same browser).
HTML, EPUB, DOCX and Markdown output need no browser.

## Workflow

### 1. Agree on the brief (ask only what you cannot default)

- Target language (required). Source language is detected.
- Output formats: default `pdf,html`; offer `epub`, `docx` (editable), `md`.
- Register and audience, e.g. "formal Modern Standard Arabic for developers".
- Translator's notes: **off by default**. Turn on only if the user asks for
  explanations or modernisation notes.
- Anything to keep untranslated (product names, terms of art).

### 2. Extract and plan

```
BT init BOOK.pdf --to ar -p my-book-ar
```
This writes `my-book-ar/` and prints the extraction report: chapters, block
counts, segments, chunk count, and chapters that look like a printed TOC,
index, credits or ads. From now on pass `-p my-book-ar` or `cd` into it.

**Check the extraction before translating a single word**: a bad extraction
can't be fixed by a good translation.
- `BT show c004 --blocks 0-40` for 2-3 chapters, compared with the same pages
  of the original. Look for merged or split paragraphs, code detected as prose,
  headings at the wrong level, lost list items, page headers that leaked in.
- Leave out chapters that are not content: `BT exclude c002 c076`.
- For PDF problems, re-run `init --force` with `--skip-pages 1-8`,
  `--columns 2`, `--chapter-level 2` or `--no-tables`
  (see `references/extraction.md`). A scanned PDF must be OCR'd first.

### 3. Settings

```
BT set style="Formal Modern Standard Arabic for software developers; keep English technical terms in parentheses on first use" formats=pdf,epub,docx
BT set notes=none numerals=keep translator="Translated by ..."
```
`style` is copied into every task, so put the register, audience and any
house conventions there.

### 4. Glossary first

Most errors in long translations are not grammar. They are one term translated
three ways, or a domain word taken in its everyday sense (for example
"artifact" rendered as an archaeological relic, or an S3 "bucket" as a folder).
Fix the vocabulary before chunk 1:
```
BT terms                                   # frequent terms, names, code tokens
BT glossary --add "bucket=حاوية (bucket)" --add "artifact=مُخرَج بناء (artifact)"
BT glossary --keep PowerShell --keep "Active Directory"
BT glossary --add "prompt (noun)=indicador"   # a sense in parentheses limits the rule to that sense
```
Pick the domain meaning of each term. Ask the user about real choices: two
accepted renderings, or a brand-name convention. Translators propose new terms
as they go (`@@ glossary`); `BT status` shows pending proposals, and
`BT glossary` lists them. Accept or correct them between batches.

### 4b. Literary books: study the voice (optional)

For novels, poetry, essays and memoirs, keep the author's style as well as the
meaning. Before chunk 1:
```
BT voice study          # writes work/voice-study.task.txt for ONE agent (web search helps)
```
That agent researches the author and the book and writes `voice/study.md`,
`voice/brief.md` (copied into every task) and `voice/people.md`. Read and fix
the brief before translating. After each chunk passes `check`,
`BT edit next` writes a voice-edit task that revises the draft toward the
author's voice; `check` then also warns if the edit lost content. Full steps:
`references/voice.md`.

### 5. Translate, chunk by chunk

Each chunk is about 2,500 source tokens. A task file contains everything the
translator needs: rules, style, the glossary entries that occur in the chunk,
the previous chunk's last lines for continuity, nearby code as context, and the
segments. **Never read `source/book.json` or whole chapters into context.**
Work only from task files.

**Alone:**
```
BT task next            # prints the task file path
```
Read the task file, write the translation file it names in the `@@ id` format,
then run `BT check N` and fix every ERROR. Re-read each WARNING's segment
against the source and fix it if the warning is right. Then `BT task next`
again. Run `BT status` whenever unsure where you are, for example after a
context reset.

**With subagents (much faster for long books):**
```
BT task next --count 4  # claims 4 chunks, prints 4 task paths
```
Start one subagent per task with the prompt in
`references/translator-prompt.md`; the task file is self-contained. When the
batch returns, run `BT check all`, review `BT glossary` proposals, then start
the next batch. Translations of the same book stay consistent because every
task carries the same glossary and style. Keep batches small at first: review
the first batch's output yourself before scaling up.

What `check` enforces:
- **Errors:** missing segments, extra or misplaced ids, empty segments.
  Anything in backticks, URLs, e-mails, link targets or keep-terms that was
  lost or changed. Segments left in the source language. Sentences dropped
  while the segment is also far shorter than the book's usual ratio.
  Translator's notes in a project with notes disabled.
- **Warnings:** missing numbers, sentence-count jumps, length outliers,
  bold/link markup that changed, and glossary terms not used.

### 6. Independent review

Mechanical checks catch omissions, not mistranslations. Before building:
```
BT review all           # writes review/NNNN.review.md (source + translation side by side)
```
Give the review sheets to fresh reviewers (subagents, ~5 chunks each) with
`references/review-rubric.md`. At minimum review every chunk that had
warnings, plus a sample from each part of the book. Fix the translation files,
then run `BT check` again. A reviewer must judge fidelity (omissions,
additions, meaning, terms, numbers, wrong-segment matches), not style.

### 7. Build and look at it

```
BT build                          # formats from settings, or --formats pdf,epub,docx,html,md
BT preview                        # contact sheet of 9 pages -> output/preview/sheet.png
BT preview --pages 1-4,37         # specific pages
```
`build` refuses to run while chunks are missing or failing. Use
`--allow-missing` only for a draft, where untranslated text is shown in the
source language. Open the preview images and check:
- the cover, and the TOC with its page numbers;
- a code page, a table, a figure;
- for right-to-left targets, that paragraphs start on the right and that
  English terms and numbers inside sentences sit in the right order;
- that every script shows real glyphs, not boxes.

Never deliver without looking at rendered pages.

### 8. Deliver

Report the output files, chunk and segment counts, the excluded chapters, the
glossary decisions, any warnings left on purpose, and the known limits. Text
inside images stays in the source language; images are copied, never redrawn.

## Rules that keep a book translation faithful

1. **Everything, once, in order.** Every segment is translated. None is
   summarised, merged, split or reordered. The checker enforces the structure;
   reviewers enforce the meaning.
2. **Code, URLs, identifiers and numbers are not translated.** Code blocks are
   copied by the builder and never pass through the translator. Inline code
   stays in backticks.
3. **Never invent content.** Do not write captions for images you have not
   seen. Translate a caption only if the source has one. Notes are opt-in, go
   in `@@ note` blocks, and must be facts the translator is sure of. When
   added notes are checked, they are the usual source of wrong claims.
4. **Translate what is written**, including the author's mistakes. Normalising
   obvious typos is fine; correcting facts is not.
5. **One term, one translation.** The glossary is binding, inflected as the
   grammar needs.
6. **Re-extracting is safe, but not free.** `init --force` after translation has
   started moves every finished translation to its new segment by matching the
   exact source text. Segments whose text changed come back untranslated, and
   `task N` then asks only for those. Re-run `check all` afterwards. To fix a
   single paragraph, edit the translation, not the source.

## Files

```
my-book-ar/
  project.json        settings (languages, style, notes, numerals, formats, exclude)
  source/book.json    extracted book (do not load into context)
  source/assets/      figures, copied byte-for-byte
  source/report.md    extraction report
  chunks.json         chunk plan: segment ids per chunk
  glossary.json       terms, keep-list, pending proposals
  work/NNNN.task.txt  task files (regenerate any time with `task N`)
  translations/NNNN.txt   the translation, one file per chunk  <- the real work
  translations/_draft/    voice mode: each chunk as it was before the voice edit
  voice/              voice mode: study.md, brief.md, people.md
  review/             review sheets and findings
  output/             book files, assets, preview/
```

## Security and permissions

- **No network access.** The scripts make no network requests and need no API
  keys; your own agent does the translation. The only URLs in the code are
  XML namespace identifiers that the EPUB 3 and XHTML specs require inside the
  output files, plus the pattern that finds links in the book's text. All of
  them are in `scripts/uris.py`. They are written as text and never fetched.
- **No environment variables** are read.
- **One external program:** for PDF output, `scripts/render.py` starts
  Microsoft Edge or Google Chrome in headless mode (`subprocess.run` with an
  argument list, no shell) to print a local HTML file. Pass `build --browser
  PATH` to choose the executable.
- **base64** is used in `scripts/extract.py` only to decode images that an
  HTML or EPUB source embeds as `data:image/...;base64,` URIs, so they can be
  saved to `source/assets/`.
- **Files:** reads the book you name and writes only inside the project folder.

More detail: `references/extraction.md` (inputs and fixing extraction),
`references/output.md` (formats, fonts, RTL and CJK),
`references/translator-prompt.md`, `references/review-rubric.md`,
`references/voice.md` (literary books).
