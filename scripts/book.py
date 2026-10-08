#!/usr/bin/env python3
"""book-translator: translate a whole book chunk by chunk without losing anything.

    python book.py init BOOK.pdf --to ar            # extract + plan chunks
    python book.py terms                             # glossary candidates
    python book.py task next                         # write the next task file
    python book.py check 7                           # verify chunk 7
    python book.py status                            # progress, what is next
    python book.py build --formats pdf,epub,docx     # assemble the translated book

Run any command with -h for options. All commands take --project DIR
(default: the current directory, or $BOOK_PROJECT).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import langs  # noqa: E402

SCRIPTS_DIR = Path(__file__).resolve().parent
TRANSLATABLE = ("heading", "para", "item", "quote", "caption")
URL_RE = re.compile(r"(?:https?|ftp)://[^\s<>\"')\]]+[^\s<>\"')\].,;:!?]|mailto:[^\s)\]]+|[\w.+-]+@[\w-]+\.[\w.-]+\w")
CODE_SPAN_RE = re.compile(r"`([^`\n]+)`")
LINK_TARGET_RE = re.compile(r"\]\(([^)\s]+)\)")
NUM_RE = re.compile(r"\d+(?:[.,:]\d+)*")
SENT_END_RE = re.compile(r"[.!?؟۔।](?=[\s\"'”»)]|$)|[。！？]")
LETTER_RE = re.compile(r"[^\W\d_]", re.U)
HEADER_RE = re.compile(r"^@@\s+(.*?)\s*$")


# ---------------------------------------------------------------------------
# project files
# ---------------------------------------------------------------------------

class Project:
    def __init__(self, root: Path):
        self.root = root.resolve()
        if not (self.root / "project.json").exists():
            raise SystemExit(f"no book-translator project in {self.root} "
                             "(run `book.py init BOOK --to LANG` or pass --project DIR)")
        self.cfg = self._load("project.json")
        self._book = None

    def _load(self, name, default=None):
        p = self.root / name
        if not p.exists():
            return default
        return json.loads(p.read_text(encoding="utf-8"))

    def save(self, name, data):
        p = self.root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, p)

    @property
    def book(self) -> dict:
        if self._book is None:
            self._book = self._load("source/book.json")
        return self._book

    @property
    def plan(self) -> dict:
        return self._load("chunks.json", {"chunks": [], "auto": []})

    @property
    def glossary(self) -> dict:
        g = self._load("glossary.json", None) or {}
        g.setdefault("terms", {})
        g.setdefault("keep", [])
        g.setdefault("pending", {})
        return g

    @property
    def state(self) -> dict:
        s = self._load("state.json", None) or {}
        s.setdefault("checked", {})
        s.setdefault("claimed", {})
        return s

    def tfile(self, n: int) -> Path:
        return self.root / "translations" / f"{n:04d}.txt"

    def taskfile(self, n: int) -> Path:
        return self.root / "work" / f"{n:04d}.task.txt"

    @property
    def src(self) -> dict:
        return langs.info(self.cfg["source_lang"])

    @property
    def tgt(self) -> dict:
        return langs.info(self.cfg["target_lang"])

    def excluded(self) -> set:
        return set(self.cfg.get("exclude", []))


def segments(book: dict, exclude: set = frozenset()) -> list[dict]:
    """Every translatable unit of the book, in reading order."""
    out = [{"id": "meta.title", "kind": "book-title", "text": book["meta"]["title"], "ch": "meta"},
           {"id": "meta.toc", "kind": "ui-label", "text": "Contents", "ch": "meta"}]
    for ch in book["chapters"]:
        cid = ch["id"]
        if cid in exclude:
            continue
        if ch["title"]:
            out.append({"id": f"{cid}.t", "kind": "chapter-title", "text": ch["title"], "ch": cid})
        for i, b in enumerate(ch["blocks"]):
            bid = f"{cid}.b{i:04d}"
            t = b["type"]
            if t in TRANSLATABLE:
                kind = f"h{b['level']}" if t == "heading" else t
                out.append({"id": bid, "kind": kind, "text": b["text"], "ch": cid})
            elif t == "table":
                for r, row in enumerate(b["rows"]):
                    for c, cell in enumerate(row):
                        if cell.strip():
                            out.append({"id": f"{bid}.r{r}c{c}", "kind": "table-cell",
                                        "text": cell, "ch": cid})
            elif t == "figure" and LETTER_RE.search(b.get("alt") or ""):
                out.append({"id": f"{bid}.alt", "kind": "image-alt", "text": b["alt"], "ch": cid})
    return out


def needs_translation(text: str) -> bool:
    """False for segments with nothing to translate (numbers, pure code, URLs)."""
    rest = CODE_SPAN_RE.sub(" ", text)
    rest = URL_RE.sub(" ", rest)
    rest = LINK_TARGET_RE.sub("]", rest)
    return bool(LETTER_RE.search(rest))


def plan_chunks(segs: list[dict], budget: int) -> dict:
    chunks, auto = [], []
    cur, tokens = [], 0

    def close():
        nonlocal cur, tokens
        if cur:
            chunks.append({"n": len(chunks) + 1, "ids": [s["id"] for s in cur], "tokens": tokens,
                           "chapters": sorted({s["ch"] for s in cur})})
        cur, tokens = [], 0

    for s in segs:
        if not needs_translation(s["text"]):
            auto.append(s["id"])
            continue
        t = langs.estimate_tokens(s["text"])
        starts_chapter = s["kind"] == "chapter-title"
        starts_section = s["kind"] in ("h1", "h2", "h3")
        if cur and (tokens + t > budget
                    or (starts_chapter and tokens > budget * 0.6)
                    or (starts_section and tokens > budget * 0.85)):
            close()
        cur.append(s)
        tokens += t
    close()
    return {"budget": budget, "chunks": chunks, "auto": auto}


# ---------------------------------------------------------------------------
# translation file format
# ---------------------------------------------------------------------------

def parse_translation(text: str) -> dict:
    """Parse a translation file.

    @@ <segment-id>          then the translated text on the following lines
    @@ <segment-id> =        keep the source text unchanged (no body)
    @@ glossary              lines "source term => translation"
    @@ note <segment-id>     a translator's note attached after that segment
    @@ -- anything           ignored (context copied from the task)
    """
    segs, keep, notes, gloss, dupes = {}, set(), {}, {}, []
    cur, buf, mode = None, [], None

    def flush():
        body = "\n".join(buf).strip()
        if mode == "seg" and cur:
            if cur in segs or cur in keep:
                dupes.append(cur)
            segs[cur] = body
        elif mode == "note" and cur and body:
            notes[cur] = body
        elif mode == "glossary":
            for line in body.splitlines():
                if "=>" in line:
                    k, v = line.split("=>", 1)
                    if k.strip() and v.strip():
                        gloss[k.strip()] = v.strip()

    for line in text.splitlines():
        m = HEADER_RE.match(line)
        if m:
            flush()
            buf = []
            parts = m.group(1).split()
            if not parts or parts[0].startswith("--"):
                cur, mode = None, None
            elif parts[0] == "glossary":
                cur, mode = None, "glossary"
            elif parts[0] == "note" and len(parts) > 1:
                cur, mode = parts[1], "note"
            else:
                cur, mode = parts[0], "seg"
                if "=" in parts[1:]:
                    keep.add(cur)
                    mode = None
            continue
        buf.append(line)
    flush()
    for k in keep:
        segs.pop(k, None)
    return {"segs": segs, "keep": keep, "notes": notes, "glossary": gloss, "dupes": dupes}


def load_all_translations(p: Project) -> dict:
    """Merge every translations/*.txt (segment id -> text or None for keep)."""
    out, notes = {}, {}
    for f in sorted((p.root / "translations").glob("*.txt")):
        t = parse_translation(f.read_text(encoding="utf-8"))
        out.update(t["segs"])
        for k in t["keep"]:
            out[k] = None
        notes.update(t["notes"])
    return {"segs": out, "notes": notes}


# ---------------------------------------------------------------------------
# init / exclude / set
# ---------------------------------------------------------------------------

def parse_pages(spec: str | None) -> list[int]:
    pages = []
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            pages.extend(range(int(a), int(b) + 1))
        else:
            pages.append(int(part))
    return pages


def cmd_init(a):
    import extract
    src = Path(a.source).resolve()
    if not src.exists():
        raise SystemExit(f"not found: {src}")
    tgt = langs.info(a.to)
    root = Path(a.project_dir or f"{re.sub(r'[^A-Za-z0-9]+', '-', src.stem).strip('-').lower()}-{tgt['code']}")
    if (root / "project.json").exists() and not a.force:
        raise SystemExit(f"{root} already has a project (use --force to re-extract; "
                         "finished translations are carried over by matching their source text)")
    old_book = old_tr = None
    if (root / "source" / "book.json").exists() and any((root / "translations").glob("*.txt")):
        old_book = json.loads((root / "source" / "book.json").read_text(encoding="utf-8"))
        old_tr = _read_translation_files(root / "translations")
    if (root / "source").exists():
        shutil.rmtree(root / "source")
    root.mkdir(parents=True, exist_ok=True)
    opts = {"skip_pages": parse_pages(a.skip_pages), "columns": a.columns,
            "chapter_level": a.chapter_level, "tables": not a.no_tables}
    data = extract.extract(src, root, opts)
    sample = " ".join(b.get("text", "") for ch in data["chapters"] for b in ch["blocks"][:40])[:20000]
    declared = data["meta"].get("language") or data["meta"].get("lang") or ""
    if a.from_lang:
        source_lang = langs.norm(a.from_lang)
    elif declared and langs.norm(declared) in langs.LANGS:
        source_lang = langs.norm(declared)
    else:
        source_lang = langs.detect(sample)
    (root / "source").mkdir(exist_ok=True)
    (root / "source" / "book.json").write_text(json.dumps(data, ensure_ascii=False, indent=1),
                                               encoding="utf-8")
    cfg = {
        "source_file": str(src), "source_lang": source_lang, "target_lang": tgt["code"],
        "chunk_tokens": a.chunk_tokens, "exclude": [], "formats": ["pdf", "html"],
        "style": "", "notes": "none", "numerals": "keep",
        "created": time.strftime("%Y-%m-%d %H:%M"),
    }
    old = root / "project.json"
    if old.exists():
        prev = json.loads(old.read_text(encoding="utf-8"))
        for k in ("style", "notes", "numerals", "formats", "exclude", "chunk_tokens", "title",
                  "author", "translator", "page_size"):
            if k in prev:
                cfg[k] = prev[k]
    (root / "project.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    for d in ("translations", "work", "review", "output"):
        (root / d).mkdir(exist_ok=True)
    p = Project(root)
    if not (root / "glossary.json").exists():
        p.save("glossary.json", {"terms": {}, "keep": [], "pending": {}})
    suggestions = suggest_excludes(p.book)
    replan(p)
    report = write_report(p, suggestions)
    print(report)
    if old_book is not None:
        print("\n" + remap_translations(p, old_book, old_tr))
    print(f"\nproject: {root}")
    if not langs.info(tgt["code"])["known"]:
        print(f"note: '{a.to}' is not in the language table; it is treated as left-to-right "
              "and the target-script check is skipped.")


def _read_translation_files(folder: Path) -> dict:
    segs, notes = {}, {}
    for f in sorted(folder.glob("*.txt")):
        t = parse_translation(f.read_text(encoding="utf-8"))
        segs.update(t["segs"])
        for k in t["keep"]:
            segs[k] = None
        notes.update(t["notes"])
    return {"segs": segs, "notes": notes}


def remap_translations(p: "Project", old_book: dict, old_tr: dict) -> str:
    """Carry finished translations over to a re-extracted book.

    Segment ids are positions, so a better extraction shifts them. Old and new
    segments are aligned by their exact source text (difflib on the sequence),
    each translation moves to its new id, and the files are rewritten per new
    chunk. Segments whose source text changed come back untranslated, and
    `task N` then asks only for those.
    """
    import difflib
    old_segs = segments(old_book)
    new_segs = segments(p.book)
    # pass 1: same kind and same text (a chapter title never matches a TOC line)
    sm = difflib.SequenceMatcher(None, [(s["kind"], s["text"]) for s in old_segs],
                                 [(s["kind"], s["text"]) for s in new_segs], autojunk=False)
    id_map = {}
    for blk in sm.get_matching_blocks():
        for k in range(blk.size):
            id_map[old_segs[blk.a + k]["id"]] = new_segs[blk.b + k]["id"]
    # pass 2: same text, kind changed (a paragraph that is now a list item or table cell)
    taken = set(id_map.values())
    by_text = {}
    for s in new_segs:
        if s["id"] not in taken:
            by_text.setdefault(s["text"], []).append(s["id"])
    for s in old_segs:
        if s["id"] not in id_map and by_text.get(s["text"]):
            id_map[s["id"]] = by_text[s["text"]].pop(0)
    moved, notes = {}, {}
    for oid, text in old_tr["segs"].items():
        if oid in id_map:
            moved[id_map[oid]] = text
    for oid, text in old_tr["notes"].items():
        if oid in id_map:
            notes[id_map[oid]] = text
    tdir = p.root / "translations"
    archive = tdir / f"_before-reinit-{time.strftime('%Y%m%d-%H%M%S')}"
    archive.mkdir(parents=True)
    for f in tdir.glob("*.txt"):
        f.replace(archive / f.name)
    written = 0
    for c in p.plan["chunks"]:
        lines = []
        for i in c["ids"]:
            if i in moved:
                lines.append(f"@@ {i} =" if moved[i] is None else f"@@ {i}\n{moved[i]}")
                written += 1
            if i in notes:
                lines.append(f"@@ note {i}\n{notes[i]}")
        if lines:
            p.tfile(c["n"]).write_text("\n\n".join(lines) + "\n", encoding="utf-8")
    st = p.state
    st["checked"], st["claimed"] = {}, {}
    p.save("state.json", st)
    lost = len(old_tr["segs"]) - len(moved)
    todo = sum(1 for c in p.plan["chunks"] for i in c["ids"] if i not in moved)
    return (f"translations carried over: {written} | old translations whose source text changed: {lost} "
            f"(archived in {archive.name}) | segments to translate now: {todo}. "
            "Run `status`; `task N` asks only for the missing segments of a started chunk.")


def suggest_excludes(book: dict) -> list[tuple[str, str]]:
    """Chapters that look like a printed table of contents, index or ads."""
    titles = {re.sub(r"\W+", "", c["title"].lower()) for c in book["chapters"] if c["title"]}
    out = []
    for c in book["chapters"]:
        texts = [b.get("text", "") for b in c["blocks"] if b["type"] in TRANSLATABLE]
        name = c["title"].lower()
        if re.search(r"\b(contents?|content list|table of contents|toc|index|credits|you may also like|"
                     r"also by|about the author|copyright|table des mati[eè]res|inhaltsverzeichnis|"
                     r"[ií]ndice|sommaire|содержание|оглавление)\b|فهرس|المحتويات|目录|目次|목차", name):
            out.append((c["id"], f"title '{c['title']}'"))
            continue
        if len(texts) >= 8:
            hits = sum(1 for t in texts if re.sub(r"\W+", "", re.sub(r"\d+\s*$", "", t).lower()) in titles
                       or re.search(r"(\.{3,}|\s)\d{1,4}\s*$", t)
                       or re.search(r"(?:\.\s?){4,}\s*\d", t)
                       or re.fullmatch(r"\s*\d{1,3}\.?\s*", t))
            if hits / len(texts) > 0.5:
                out.append((c["id"], f"{hits}/{len(texts)} lines are chapter titles or end in page numbers"))
    return out


def replan(p: Project) -> None:
    segs = segments(p.book, p.excluded())
    plan = plan_chunks(segs, int(p.cfg.get("chunk_tokens", 2500)))
    p.save("chunks.json", plan)


def write_report(p: Project, suggestions) -> str:
    b = p.book
    types = Counter(x["type"] for ch in b["chapters"] for x in ch["blocks"])
    segs = segments(b, p.excluded())
    words = sum(langs.word_count(s["text"]) for s in segs)
    toks = sum(langs.estimate_tokens(s["text"]) for s in segs)
    plan = p.plan
    labelled = [(ch["id"], x) for ch in b["chapters"] for x in ch["blocks"]
                if x["type"] == "figure" and x.get("labels")]
    lines = [
        f"# Extraction report: {b['meta']['title']}",
        "",
        f"- source: {b['meta']['source']} ({b['meta']['format']}), "
        f"{langs.info(p.cfg['source_lang'])['name']} -> {p.tgt['name']} ({p.tgt['dir']})",
        f"- chapters: {len(b['chapters'])} | blocks: " + ", ".join(f"{k} {v}" for k, v in types.most_common()),
        f"- to translate: {len(segs) - len(plan['auto'])} segments, ~{words:,} words, ~{toks:,} tokens "
        f"in {len(plan['chunks'])} chunks of <= {plan['budget']} tokens "
        f"({len(plan['auto'])} segments need no translation: numbers, code-only, URLs)",
        f"- code blocks are copied verbatim ({types.get('code', 0)}); figures are copied as images "
        f"({types.get('figure', 0)}, {len(labelled)} contain text that stays untranslated in the image)",
        "",
        "## Chapters",
    ]
    for ch in b["chapters"]:
        n = len(ch["blocks"])
        flag = " [excluded]" if ch["id"] in p.excluded() else ""
        lines.append(f"- {ch['id']}: {ch['title'][:70] or '(untitled)'} - {n} blocks{flag}")
    if suggestions:
        lines += ["", "## Consider excluding (`book.py exclude ID ...`)"]
        lines += [f"- {cid}: {why}" for cid, why in suggestions]
    if b.get("notes"):
        lines += ["", "## Extraction notes"] + [f"- {n}" for n in b["notes"][:20]]
    text = "\n".join(lines)
    (p.root / "source" / "report.md").write_text(text + "\n", encoding="utf-8")
    return text


def cmd_exclude(a):
    p = Project(a.project_dir)
    ex = set(p.cfg.get("exclude", []))
    ids = {c["id"] for c in p.book["chapters"]}
    for cid in a.ids:
        if cid not in ids:
            raise SystemExit(f"unknown chapter id {cid} (see source/report.md)")
        if a.include:
            ex.discard(cid)
        else:
            ex.add(cid)
    p.cfg["exclude"] = sorted(ex)
    p.save("project.json", p.cfg)
    started = any((p.root / "translations").glob("*.txt"))
    if started:
        print("translation already started: chunk plan kept, excluded chapters are skipped "
              "by status/check/build")
    else:
        replan(p)
        print(f"re-planned: {len(p.plan['chunks'])} chunks")
    print("excluded:", ", ".join(sorted(ex)) or "none")


def cmd_set(a):
    p = Project(a.project_dir)
    allowed = {"style", "notes", "numerals", "formats", "chunk_tokens", "title", "author",
               "translator", "page_size", "source_lang"}
    for kv in a.pairs:
        if "=" not in kv:
            raise SystemExit(f"expected key=value, got {kv!r}")
        k, v = kv.split("=", 1)
        k = k.strip()
        if k not in allowed:
            raise SystemExit(f"unknown setting {k}; allowed: {', '.join(sorted(allowed))}")
        if k == "formats":
            v = [x.strip() for x in v.split(",") if x.strip()]
        elif k == "chunk_tokens":
            v = int(v)
        elif k == "notes" and v not in ("none", "allowed"):
            raise SystemExit("notes must be 'none' or 'allowed'")
        elif k == "numerals" and v not in ("keep", "native"):
            raise SystemExit("numerals must be 'keep' or 'native'")
        p.cfg[k] = v
    p.save("project.json", p.cfg)
    if any(kv.startswith("chunk_tokens") for kv in a.pairs):
        if any((p.root / "translations").glob("*.txt")):
            print("chunk_tokens saved but not applied: translation already started")
        else:
            replan(p)
    print(json.dumps({k: p.cfg[k] for k in p.cfg if k in allowed}, ensure_ascii=False, indent=1))


# ---------------------------------------------------------------------------
# glossary
# ---------------------------------------------------------------------------

STOP_EN = set("""a an the and or but if then else of to in on at by for with from as is are was were be been
being this that these those it its into than so such not no can will would should could may might must do
does did done have has had you your we our they their he she his her them there here what which who whom when
where why how all any each every some most more many much few other another use using used example section
chapter note also see following first second new one two three only just like very""".split())


def cmd_terms(a):
    p = Project(a.project_dir)
    segs = [s for s in segments(p.book, p.excluded()) if s["id"] not in set(p.plan["auto"])]
    code, caps, words, heads = Counter(), Counter(), Counter(), Counter()
    context = {}
    for s in segs:
        t = s["text"]
        for c in CODE_SPAN_RE.findall(t):
            if len(c) <= 40:
                code[c] += 1
        plain = CODE_SPAN_RE.sub(" ", URL_RE.sub(" ", t))
        plain = re.sub(r"[*\[\]\\]", "", LINK_TARGET_RE.sub("]", plain))
        for m in re.finditer(r"(?<![.!?]\s)(?<!^)\b([A-Z][\w+#.-]*[A-Za-z0-9+#](?:\s+[A-Z][\w+#.-]*[A-Za-z0-9+#]){0,3})", plain):
            caps[m.group(1)] += 1
            context.setdefault(m.group(1), plain[max(0, m.start() - 50): m.end() + 50])
        toks = [w.lower() for w in re.findall(r"[^\W\d_][\w'-]+", plain)]
        for i, w in enumerate(toks):
            if len(w) >= 5 and w not in STOP_EN:
                words[w] += 1
                context.setdefault(w, plain[:140])
            if i + 1 < len(toks) and toks[i] not in STOP_EN and toks[i + 1] not in STOP_EN \
                    and len(toks[i]) > 3 and len(toks[i + 1]) > 3:
                words[f"{toks[i]} {toks[i + 1]}"] += 1
        if s["kind"] in ("chapter-title", "h1", "h2", "h3"):
            for w in re.findall(r"[^\W\d_][\w'-]{3,}", plain.lower()):
                if w not in STOP_EN:
                    heads[w] += 1
    g = p.glossary
    known = set(k.lower() for k in g["terms"]) | set(k.lower() for k in g["keep"])
    top = a.top
    print("## Candidate terms (decide one translation each, then add them to glossary.json)")
    print("# score = occurrences; heading words are weighted x3")
    scored = Counter()
    for w, n in words.items():
        if n >= 3:
            scored[w] = n + 3 * heads.get(w, 0)
    for w, n in scored.most_common(top):
        if w.lower() in known:
            continue
        print(f"{n:5d}  {w}")
    print("\n## Proper names / product names (usually kept as-is or transliterated consistently)")
    for w, n in caps.most_common(top):
        if n >= 3 and w.lower() not in known and w.lower() not in STOP_EN:
            print(f"{n:5d}  {w}    | {context[w][:90]!r}")
    print("\n## Inline code tokens (always kept verbatim - no action needed)")
    print(", ".join(f"{c} ({n})" for c, n in code.most_common(25)) or "none")


def cmd_glossary(a):
    p = Project(a.project_dir)
    g = p.glossary
    changed = False
    for item in a.add or []:
        if "=" not in item:
            raise SystemExit("--add expects 'term=translation'")
        k, v = item.split("=", 1)
        g["terms"][k.strip()] = v.strip()
        g["pending"].pop(k.strip(), None)
        changed = True
    for item in a.keep or []:
        if item not in g["keep"]:
            g["keep"].append(item)
            changed = True
    for k in a.remove or []:
        g["terms"].pop(k, None)
        g["pending"].pop(k, None)
        if k in g["keep"]:
            g["keep"].remove(k)
        changed = True
    if a.accept_all:
        for k, v in g["pending"].items():
            g["terms"].setdefault(k, v["translation"])
        g["pending"] = {}
        changed = True
    if changed:
        p.save("glossary.json", g)
    print(f"terms: {len(g['terms'])} | keep: {len(g['keep'])} | pending proposals: {len(g['pending'])}")
    for k, v in sorted(g["terms"].items(), key=lambda kv: kv[0].lower()):
        print(f"  {k} => {v}")
    if g["keep"]:
        print("  keep as-is: " + ", ".join(g["keep"]))
    if g["pending"]:
        print("pending (accept with --accept-all, or --add 'term=better translation'):")
        for k, v in g["pending"].items():
            print(f"  {k} => {v['translation']}   (chunk {v['chunk']})")


def glossary_for(text: str, g: dict) -> list[tuple[str, str]]:
    # terms inside code, URLs or paths (`/etc/os-release`) are not prose: skip them
    prose = CODE_SPAN_RE.sub(" ", URL_RE.sub(" ", LINK_TARGET_RE.sub("]", text)))
    prose = re.sub(r"\S*[/\\]\S*", " ", prose)
    low = prose.lower()
    hits = []
    for k, v in g["terms"].items():
        word = re.sub(r"\s*\([^)]*\)\s*$", "", k).lower()    # "prompt (noun)" matches "prompt"
        if word and re.search(rf"(?<!\w){re.escape(word)}(?!\w)", low):
            hits.append((k, v))
    return hits


# ---------------------------------------------------------------------------
# tasks
# ---------------------------------------------------------------------------

RULES = """1. Translate EVERY segment completely and faithfully: every sentence, clause, list item,
   number, caveat and example. Never summarize, shorten, merge, split or reorder segments,
   and never add explanations that are not in the source.
2. Keep VERBATIM: anything in `backticks`, URLs, e-mail addresses, file paths, code
   identifiers, command names, version numbers, and the "keep as-is" terms below.
3. Keep the inline markup: **bold**, *italic*, `code`, [link text](url). Translate the
   link text, never the url. Keep backslash escapes such as \\* exactly.
4. Use the glossary rendering for every listed term. Inflect it as the grammar needs
   (plural, case, prefixes, articles) but keep its core wording. A term marked with a
   sense, e.g. "prompt (noun)", applies only in that sense: translate other senses
   naturally and ignore the glossary warning for them - never twist a sentence to silence
   a warning. If you meet a recurring technical term that is not in the glossary,
   propose one under "@@ glossary".
5. Translate meaning, not word order: the result must read naturally in {tgt}, in the
   register described under "Style". Proper names: keep or transliterate consistently.
6. Never correct facts, claims or examples - translate them as written, even if wrong.
   Obvious typos and stray spaces in the source may simply be normalised.{notes_rule}
7. Code blocks are shown only as context; they are copied into the book unchanged.
8. Use "=" (keep the source) only for segments with nothing to translate: names, numbers,
   dates, versions, commands, or literal program output / column headers that the reader
   sees on screen exactly like that. Everything else gets translated.
"""

FORMAT = """\
Write the file {out} in exactly this format (UTF-8 text, not JSON):

@@ <segment-id>
<translation of that segment>
(the header holds the id only; the kind shown after it in the task is optional)

@@ <segment-id> =
(a segment that must stay exactly as in the source: write "=" and no text)

Optional, at the end:
@@ glossary
<source term> => <translation>

Every segment id below must appear exactly once. Do not copy the "@@ --" context lines.
When the file is written, run:
    python "{script}" check {n} --project "{project}"
and fix every ERROR it reports (WARNINGS: re-read the segment and fix it if it is right).
"""


def build_task(p: Project, n: int) -> str:
    plan = p.plan
    chunk = next((c for c in plan["chunks"] if c["n"] == n), None)
    if chunk is None:
        raise SystemExit(f"no chunk {n} (plan has {len(plan['chunks'])})")
    segs = {s["id"]: s for s in segments(p.book)}
    ids = [i for i in chunk["ids"] if segs.get(i, {}).get("ch") not in p.excluded()]
    partial = False
    if p.tfile(n).exists():
        done = parse_translation(p.tfile(n).read_text(encoding="utf-8"))
        missing = [i for i in ids if i not in done["segs"] and i not in done["keep"]]
        if missing and len(missing) < len(ids):
            ids, partial = missing, True
    g = p.glossary
    text_all = " ".join(segs[i]["text"] for i in ids)
    gl = glossary_for(text_all, g)
    keep = [k for k in g["keep"] if k.lower() in text_all.lower()]
    tgt, src = p.tgt, p.src
    notes_rule = ("\n   Translator's notes are allowed ONLY for facts a reader of the translation"
                  "\n   needs; write them as \"@@ note <segment-id>\" blocks, never inside a"
                  "\n   segment, and only state things you are sure are true."
                  if p.cfg.get("notes") == "allowed" else
                  "\n   Do not add translator's notes.")
    numerals = ("Write numbers with the native digits of the target language (keep digits in code,"
                " versions and identifiers)." if p.cfg.get("numerals") == "native"
                else "Keep numbers in the digits used by the source.")
    out_path = p.tfile(n)
    head = [
        f"# book-translator task {n:04d} of {len(plan['chunks']):04d}",
        f"Book: {p.book['meta']['title']}",
        f"Translate from {src['name']} ({src['code']}) into {tgt['name']} ({tgt['code']}, "
        f"{'right-to-left' if tgt['dir'] == 'rtl' else 'left-to-right'}).",
        f"Segments: {len(ids)} | about {chunk['tokens']} source tokens",
        "",
        "## Rules",
        RULES.format(tgt=tgt["name"], notes_rule=notes_rule).rstrip(),
        f"9. {numerals}",
        "",
        "## Style",
        p.cfg.get("style") or f"Clear, natural, standard {tgt['name']} suitable for a published book.",
        "",
        "## Glossary for this chunk (mandatory renderings)",
    ]
    head += [f"{k} => {v}" for k, v in gl] or ["(none yet)"]
    head += ["", "## Keep as-is", ", ".join(keep) or "(none)", "", "## Output", FORMAT.format(
        out=out_path, script=SCRIPTS_DIR / "book.py", n=n, project=p.root).rstrip(), ""]
    if partial:
        head += ["## IMPORTANT: this chunk is partly translated",
                 f"{out_path} already holds the other segments of this chunk. Translate ONLY the "
                 f"{len(ids)} segments below and APPEND them to the end of that file. Do not change or "
                 "repeat the entries already in it.", ""]

    # continuity: the last translated segments of the previous chunk
    prev = next((c for c in plan["chunks"] if c["n"] == n - 1), None)
    if prev and p.tfile(n - 1).exists():
        tr = parse_translation(p.tfile(n - 1).read_text(encoding="utf-8"))["segs"]
        tail = [i for i in prev["ids"] if i in tr][-2:]
        if tail:
            head += ["## Previous translated text (context only, for continuity)"]
            head += [f"@@ -- {i}\n{tr[i]}" for i in tail] + [""]

    # segments, with nearby code blocks as context
    head.append("## Segments")
    blocks_by_ch = {ch["id"]: ch["blocks"] for ch in p.book["chapters"]}
    shown_code = set()
    body = []
    def code_context(cid, blocks, js):
        for j in js:
            shown_code.add((cid, j))
            code_lines = blocks[j]["text"].splitlines()
            snippet = "\n".join(code_lines[:6]) + ("\n..." if len(code_lines) > 6 else "")
            body.append(f"@@ -- code (context only, copied unchanged)\n{snippet}")

    for i in ids:
        s = segs[i]
        m = re.match(r"(c\d+)\.b(\d+)$", i)
        if m:   # code just before the paragraph ("... as shown above")
            cid, bi = m.group(1), int(m.group(2))
            blocks = blocks_by_ch.get(cid, [])
            j, before = bi - 1, []
            while j >= 0 and blocks[j]["type"] == "code" and (cid, j) not in shown_code:
                before.insert(0, j)
                j -= 1
            code_context(cid, blocks, before)
        body.append(f"@@ {i} {s['kind']}\n{s['text']}")
        if m:   # and just after it ("Run this command:")
            j, after = bi + 1, []
            while j < len(blocks) and blocks[j]["type"] == "code" and (cid, j) not in shown_code:
                after.append(j)
                j += 1
            code_context(cid, blocks, after)
    return "\n".join(head) + "\n" + "\n\n".join(body) + "\n"


def cmd_task(a):
    p = Project(a.project_dir)
    st = p.state
    plan = p.plan
    nums = []
    if a.which == "next":
        for c in plan["chunks"]:
            key = f"{c['n']:04d}"
            if key in st["claimed"] and not a.reclaim:
                continue
            if p.tfile(c["n"]).exists():
                done = parse_translation(p.tfile(c["n"]).read_text(encoding="utf-8"))
                if all(i in done["segs"] or i in done["keep"] for i in c["ids"]
                       if not (c["chapters"] and set(c["chapters"]) <= p.excluded())):
                    continue
            if c["chapters"] and set(c["chapters"]) <= p.excluded():
                continue
            nums.append(c["n"])
            if len(nums) >= a.count:
                break
        if not nums:
            print("no unclaimed chunks without a translation. "
                  "Run `status` (use --reclaim to hand out claimed chunks again).")
            return
    else:
        nums = [int(x) for x in a.which.split(",")]
    for n in nums:
        text = build_task(p, n)
        f = p.taskfile(n)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
        st["claimed"][f"{n:04d}"] = time.strftime("%Y-%m-%d %H:%M")
        print(f"task {n:04d}: {f}  ->  write {p.tfile(n)}  (~{langs.estimate_tokens(text)} tokens)")
        if a.print:
            print(text)
    p.save("state.json", st)


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------

def verbatim_tokens(text: str, keep_terms) -> list[str]:
    toks = [f"`{c}`" for c in CODE_SPAN_RE.findall(text) if re.search(r"\w", c)]
    plain = CODE_SPAN_RE.sub(" ", text)
    toks += LINK_TARGET_RE.findall(plain)
    toks += [u for u in URL_RE.findall(LINK_TARGET_RE.sub("]", plain))]
    for k in keep_terms:
        if re.search(rf"(?<!\w){re.escape(k)}(?!\w)", plain):
            toks.append(k)
    return toks


def numbers(text: str) -> Counter:
    t = CODE_SPAN_RE.sub(" ", URL_RE.sub(" ", LINK_TARGET_RE.sub("]", text)))
    return Counter(langs.ascii_digits(m) for m in NUM_RE.findall(langs.ascii_digits(t)))


def sentences(text: str) -> int:
    t = CODE_SPAN_RE.sub("x", URL_RE.sub("x", text))
    t = re.sub(r"\b(e\.g|i\.e|etc|vs|cf|Dr|Mr|Mrs|Ms|St|No|Fig|approx)\.", r"\1", t)
    t = re.sub(r"\d\.\d", "0", t)
    return len(SENT_END_RE.findall(t))


def ratio_baseline(p: Project, all_tr: dict, segs: dict) -> float:
    rs = []
    for i, tr in all_tr.items():
        if tr and i in segs and len(segs[i]["text"]) >= 80:
            rs.append(len(tr) / len(segs[i]["text"]))
    return statistics.median(rs) if len(rs) >= 20 else 0.0


def check_chunk(p: Project, n: int, segs: dict, baseline: float, chunk_of: dict) -> tuple[list, list]:
    errors, warns = [], []
    plan = p.plan
    chunk = next((c for c in plan["chunks"] if c["n"] == n), None)
    if chunk is None:
        return [f"no chunk {n}"], []
    f = p.tfile(n)
    if not f.exists():
        return [f"missing file {f}"], []
    tr = parse_translation(f.read_text(encoding="utf-8"))
    excluded = p.excluded()
    want = [i for i in chunk["ids"] if segs.get(i, {}).get("ch") not in excluded]
    got = set(tr["segs"]) | tr["keep"]
    g = p.glossary
    tgt = p.tgt
    tre = langs.script_re([s for s in tgt["scripts"] if s != "latin"]) if tgt["scripts"] else None

    for d in tr["dupes"]:
        errors.append(f"{d}: appears more than once")
    for i in want:
        if i not in got:
            errors.append(f"{i}: missing")
    for i in sorted(got - set(want)):
        other = chunk_of.get(i)
        errors.append(f"{i}: not part of this chunk" + (f" (belongs to chunk {other})" if other else
                                                        " (unknown id - typo?)"))
    if tr["notes"] and p.cfg.get("notes") != "allowed":
        errors.append(f"translator's notes are disabled for this project ({len(tr['notes'])} found); "
                      "remove them or `book.py set notes=allowed`")
    for i in tr["notes"]:
        if i not in want:
            errors.append(f"note for {i}: not a segment of this chunk")

    for i in want:
        if i not in tr["segs"]:
            continue
        s, t = segs[i]["text"], tr["segs"][i]
        if not t.strip():
            errors.append(f"{i}: empty translation (use '@@ {i} =' to keep the source)")
            continue
        if HEADER_RE.match(t) or "\n@@" in t:
            errors.append(f"{i}: contains an '@@' header line - format error")
        for tok in verbatim_tokens(s, g["keep"]):
            if tok not in t:
                errors.append(f"{i}: verbatim token lost or changed: {tok}")
        src_words = len(re.findall(r"[^\W\d_]{2,}", CODE_SPAN_RE.sub(" ", s)))
        if t.strip() == s.strip() and src_words >= 4:
            errors.append(f"{i}: identical to the source - not translated "
                          f"(write '@@ {i} =' if it must stay as is)")
        elif t.strip() == s.strip() and src_words >= 2:
            warns.append(f"{i}: identical to the source")
        if tre is not None and src_words >= 2 and not tre.search(CODE_SPAN_RE.sub(" ", t)):
            errors.append(f"{i}: no {tgt['name']} text in the translation")
        # markup balance
        for mark, label in (("**", "bold"), ("](", "link")):
            if s.count(mark) != t.count(mark):
                warns.append(f"{i}: {label} markup count differs ({s.count(mark)} -> {t.count(mark)})")
        # numbers
        if p.cfg.get("numerals") in ("keep", "native"):
            miss = numbers(s) - numbers(t)
            if miss:
                warns.append(f"{i}: numbers missing: {', '.join(sorted(miss))}")
        # sentence count (dropped or added sentences)
        ns, nt = sentences(s), sentences(t)
        short = bool(baseline) and len(s) >= 80 and len(t) / len(s) < baseline * 0.6
        if ns >= 3 and nt < ns * 0.5:
            msg = f"{i}: {ns} sentences in the source, {nt} in the translation"
            if short:
                errors.append(msg + " and much shorter than usual - sentences dropped")
            else:
                warns.append(msg + " - something dropped?")
        if nt >= 4 and nt > max(ns, 1) * 2:
            warns.append(f"{i}: {nt} sentences vs {ns} in the source - something added?")
        # length ratio against the book's own median
        if baseline and len(s) >= 80:
            r = len(t) / len(s)
            if r < baseline * 0.45:
                warns.append(f"{i}: much shorter than usual ({r:.2f} vs median {baseline:.2f}) - omission?")
            elif r > baseline * 2.2:
                warns.append(f"{i}: much longer than usual ({r:.2f} vs median {baseline:.2f}) - addition?")
        # glossary
        for k, v in glossary_for(s, g):
            if v.lower() not in t.lower():
                hint = " (fine if the word is used in another sense here)" if k.rstrip().endswith(")") else ""
                warns.append(f"{i}: glossary term '{k}' not rendered as '{v}'{hint}")
    # glossary proposals
    if tr["glossary"]:
        pend = g["pending"]
        changed = False
        for k, v in tr["glossary"].items():
            if k not in g["terms"] and k not in pend:
                pend[k] = {"translation": v, "chunk": n}
                changed = True
            elif k in g["terms"] and g["terms"][k] != v:
                warns.append(f"glossary proposal '{k} => {v}' conflicts with '{g['terms'][k]}'")
        if changed:
            p.save("glossary.json", g)
    return errors, warns


def cmd_check(a):
    p = Project(a.project_dir)
    plan = p.plan
    segs = {s["id"]: s for s in segments(p.book)}
    chunk_of = {i: c["n"] for c in plan["chunks"] for i in c["ids"]}
    all_tr = load_all_translations(p)["segs"]
    baseline = ratio_baseline(p, all_tr, segs)
    if a.which == "all":
        nums = [c["n"] for c in plan["chunks"] if p.tfile(c["n"]).exists()]
        missing = [c["n"] for c in plan["chunks"] if not p.tfile(c["n"]).exists()]
    else:
        nums = [int(x) for x in a.which.split(",")]
        missing = []
    st = p.state
    total_e = total_w = 0
    for n in nums:
        e, w = check_chunk(p, n, segs, baseline, chunk_of)
        total_e += len(e)
        total_w += len(w)
        f = p.tfile(n)
        st["checked"][f"{n:04d}"] = {"errors": len(e), "warnings": len(w),
                                     "mtime": f.stat().st_mtime if f.exists() else 0}
        if e or w or a.which != "all":
            status = "OK" if not e else "FAIL"
            print(f"chunk {n:04d}: {status} - {len(e)} errors, {len(w)} warnings")
            for x in e:
                print(f"  ERROR {x}")
            for x in w[: a.max_warnings]:
                print(f"  WARN  {x}")
            if len(w) > a.max_warnings:
                print(f"  ... {len(w) - a.max_warnings} more warnings (--max-warnings N)")
    p.save("state.json", st)
    if a.which == "all":
        print(f"checked {len(nums)} chunks: {total_e} errors, {total_w} warnings"
              + (f"; {len(missing)} chunks not translated yet" if missing else ""))
    sys.exit(1 if total_e else 0)


# ---------------------------------------------------------------------------
# status / review
# ---------------------------------------------------------------------------

def cmd_status(a):
    p = Project(a.project_dir)
    plan, st = p.plan, p.state
    done = ok = 0
    stale, failing, claimed = [], [], []
    pending = []
    for c in plan["chunks"]:
        key = f"{c['n']:04d}"
        f = p.tfile(c["n"])
        if f.exists():
            done += 1
            chk = st["checked"].get(key)
            if not chk or chk.get("mtime", 0) < f.stat().st_mtime - 0.5:
                stale.append(c["n"])
            elif chk["errors"]:
                failing.append(c["n"])
            else:
                ok += 1
        elif key in st["claimed"]:
            claimed.append(c["n"])
        else:
            pending.append(c["n"])
    total = len(plan["chunks"])
    g = p.glossary
    print(f"{p.book['meta']['title']} | {p.src['name']} -> {p.tgt['name']}")
    print(f"chunks: {total} | translated: {done} | checked OK: {ok} | "
          f"failing: {len(failing)} | not checked since edit: {len(stale)} | "
          f"claimed: {len(claimed)} | not started: {len(pending)}")
    pct = 100 * ok / total if total else 0
    print(f"progress: {pct:.0f}% verified")

    def rng(xs):
        return ", ".join(str(x) for x in xs[:20]) + (" ..." if len(xs) > 20 else "")
    if failing:
        print(f"fix: {rng(failing)}")
    if stale:
        print(f"re-check: {rng(stale)}")
    if claimed:
        print(f"claimed (in progress or abandoned): {rng(claimed)}")
    if pending:
        print(f"next: {rng(pending)}")
    print(f"glossary: {len(g['terms'])} terms, {len(g['pending'])} pending proposals"
          + (" -> review with `glossary`" if g["pending"] else ""))
    if ok == total and total:
        print("all chunks verified -> run the review pass, then `build`")


def cmd_review(a):
    p = Project(a.project_dir)
    plan = p.plan
    segs = {s["id"]: s for s in segments(p.book)}
    nums = [c["n"] for c in plan["chunks"]] if a.which == "all" else [int(x) for x in a.which.split(",")]
    outs = []
    for n in nums:
        f = p.tfile(n)
        if not f.exists():
            continue
        tr = parse_translation(f.read_text(encoding="utf-8"))
        chunk = next(c for c in plan["chunks"] if c["n"] == n)
        lines = [f"# Review chunk {n:04d}: {p.src['name']} -> {p.tgt['name']}", "",
                 "Compare each pair for: omission, addition, meaning change, wrong term, "
                 "changed code/number/name, segment matched to the wrong source. "
                 f"Rubric: {SCRIPTS_DIR.parent / 'references' / 'review-rubric.md'}", ""]
        for i in chunk["ids"]:
            if i not in segs or segs[i]["ch"] in p.excluded():
                continue
            t = tr["segs"].get(i)
            lines += [f"## {i} ({segs[i]['kind']})", f"SRC: {segs[i]['text']}",
                      f"TGT: {'(kept as source)' if i in tr['keep'] else (t if t is not None else '(MISSING)')}"]
            if i in tr["notes"]:
                lines.append(f"NOTE: {tr['notes'][i]}")
            lines.append("")
        out = p.root / "review" / f"{n:04d}.review.md"
        out.write_text("\n".join(lines), encoding="utf-8")
        outs.append(out)
    for o in outs:
        print(o)


def cmd_show(a):
    """Print a chapter's extracted blocks compactly, to compare with the original."""
    p = Project(a.project_dir)
    ch = next((c for c in p.book["chapters"] if c["id"] == a.chapter), None)
    if ch is None:
        raise SystemExit(f"no chapter {a.chapter}; ids are listed in source/report.md")
    lo, hi = 0, len(ch["blocks"])
    if a.blocks:
        x, _, y = a.blocks.partition("-")
        lo, hi = int(x), int(y or x) + 1
    tr = load_all_translations(p)["segs"] if a.translated else {}
    print(f"{ch['id']}: {ch['title']}  ({len(ch['blocks'])} blocks)")
    for i in range(lo, min(hi, len(ch["blocks"]))):
        b = ch["blocks"][i]
        bid = f"{ch['id']}.b{i:04d}"
        t = b["type"]
        if t == "code":
            first = b["text"].splitlines()[0] if b["text"] else ""
            body = f"{first[:90]}  [+{b['text'].count(chr(10))} lines]"
        elif t == "table":
            body = f"{len(b['rows'])}x{max(len(r) for r in b['rows'])}: " + " | ".join(b["rows"][0])[:100]
        elif t == "figure":
            body = f"{b['file']}" + (f"  labels: {', '.join(b['labels'][:6])[:80]}" if b.get("labels") else "")
        else:
            body = b["text"][:160]
        lvl = f"h{b['level']}" if t == "heading" else t
        print(f"[{i:4d}] {lvl:7s} {body}")
        if bid in tr and tr[bid]:
            print(f"        -> {tr[bid][:160]}")


def cmd_doctor(a):
    ok = True
    print(f"python {sys.version.split()[0]}")
    for mod, why, need in (("pymupdf", "PDF input, previews, PDF bookmarks", True),
                           ("docx", "DOCX input/output (pip install python-docx)", False),
                           ("PIL", "DOCX images, preview sheets (pip install pillow)", False),
                           ("playwright", "page numbers in PDF footers (optional)", False)):
        try:
            __import__(mod)
            print(f"  ok       {mod:10s} {why}")
        except ImportError:
            print(f"  {'MISSING' if need else 'optional'} {mod:10s} {why}")
            ok = ok and not need
    import render
    br = render._browser_candidates()
    print(f"  {'ok' if br else 'MISSING':8s} browser    PDF output: {br[0] if br else 'install Edge/Chrome or set BOOK_BROWSER'}")
    sys.exit(0 if ok else 1)


# ---------------------------------------------------------------------------
# build / preview
# ---------------------------------------------------------------------------

def cmd_build(a):
    import render
    p = Project(a.project_dir)
    plan = p.plan
    missing = [c["n"] for c in plan["chunks"] if not p.tfile(c["n"]).exists()]
    st = p.state
    failing = [k for k, v in st["checked"].items() if v.get("errors")]
    if missing and not a.allow_missing:
        raise SystemExit(f"{len(missing)} chunks are not translated yet ({missing[:10]}...). "
                         "Translate them, or pass --allow-missing for a draft (source text is used).")
    if failing and not a.force:
        raise SystemExit(f"chunks with check errors: {failing[:10]}. Fix them or pass --force.")
    formats = [x.strip() for x in (a.formats or ",".join(p.cfg.get("formats", ["pdf", "html"]))).split(",")]
    tr = load_all_translations(p)
    outs = render.build(p, tr, formats, page_size=a.page_size or p.cfg.get("page_size", "A4"))
    for o in outs:
        print(o)


def cmd_preview(a):
    import pymupdf
    p = Project(a.project_dir)
    pdfs = sorted((p.root / "output").glob("*.pdf"))
    if not pdfs:
        raise SystemExit("no PDF in output/ - run `build --formats pdf` first")
    doc = pymupdf.open(pdfs[0])
    out = p.root / "output" / "preview"
    out.mkdir(parents=True, exist_ok=True)
    if a.pages:
        pages = [x - 1 for x in parse_pages(a.pages) if 0 < x <= doc.page_count]
    else:
        k = min(a.sheet, doc.page_count)
        pages = sorted({round(i * (doc.page_count - 1) / max(k - 1, 1)) for i in range(k)})
    files = []
    for i in pages:
        pix = doc[i].get_pixmap(dpi=a.dpi)
        f = out / f"page_{i + 1:04d}.png"
        pix.save(str(f))
        files.append(f)
    if len(files) > 1 and not a.pages:
        from PIL import Image
        ims = [Image.open(f) for f in files]
        cols = 3
        w, h = ims[0].size
        rows = (len(ims) + cols - 1) // cols
        sheet = Image.new("RGB", (cols * w, rows * h), "white")
        for k, im in enumerate(ims):
            sheet.paste(im, ((k % cols) * w, (k // cols) * h))
        sf = out / "sheet.png"
        sheet.save(sf)
        print(sf)
    for f in files:
        print(f)
    print(f"{pdfs[0].name}: {doc.page_count} pages")


# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(prog="book.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--project", "-p", dest="project_dir", default=None,
                        help="project directory (default: current directory or $BOOK_PROJECT)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    _add = sub.add_parser

    def add_parser(name, **kw):
        return _add(name, parents=[common], **kw)
    sub.add_parser = add_parser

    s = sub.add_parser("init", help="extract a book and plan the chunks")
    s.add_argument("source")
    s.add_argument("--to", required=True, help="target language code, e.g. ar, fr, zh, he")
    s.add_argument("--from", dest="from_lang", help="source language code (default: detected)")
    s.add_argument("--chunk-tokens", type=int, default=2500,
                   help="source tokens per chunk (default 2500; lower for weaker models)")
    s.add_argument("--skip-pages", help="PDF pages to ignore, e.g. 1-8,182-184")
    s.add_argument("--columns", type=int, default=1, help="PDF text columns per page")
    s.add_argument("--chapter-level", type=int, help="outline/heading level that starts a chapter")
    s.add_argument("--no-tables", action="store_true", help="disable PDF table detection")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("exclude", help="leave chapters out (printed TOC, index, ads...)")
    s.add_argument("ids", nargs="+")
    s.add_argument("--include", action="store_true", help="put them back")
    s.set_defaults(fn=cmd_exclude)

    s = sub.add_parser("set", help="project settings: style, notes, numerals, formats, title, ...")
    s.add_argument("pairs", nargs="+", metavar="key=value")
    s.set_defaults(fn=cmd_set)

    s = sub.add_parser("terms", help="list glossary candidates")
    s.add_argument("--top", type=int, default=50)
    s.set_defaults(fn=cmd_terms)

    s = sub.add_parser("glossary", help="show / edit the glossary")
    s.add_argument("--add", action="append", metavar="TERM=TRANSLATION")
    s.add_argument("--keep", action="append", metavar="TERM", help="term to keep untranslated")
    s.add_argument("--remove", action="append", metavar="TERM")
    s.add_argument("--accept-all", action="store_true", help="accept all pending proposals")
    s.set_defaults(fn=cmd_glossary)

    s = sub.add_parser("task", help="write task file(s): `task next`, `task next --count 4`, `task 7`")
    s.add_argument("which", nargs="?", default="next")
    s.add_argument("--count", type=int, default=1)
    s.add_argument("--print", action="store_true", help="also print the task text")
    s.add_argument("--reclaim", action="store_true", help="hand out claimed chunks again")
    s.set_defaults(fn=cmd_task)

    s = sub.add_parser("check", help="verify chunk(s): `check 7`, `check 3,4`, `check all`")
    s.add_argument("which")
    s.add_argument("--max-warnings", type=int, default=25)
    s.set_defaults(fn=cmd_check)

    s = sub.add_parser("status", help="progress and what to do next")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("review", help="write bilingual review sheets: `review 7` or `review all`")
    s.add_argument("which")
    s.set_defaults(fn=cmd_review)

    s = sub.add_parser("show", help="print a chapter's extracted blocks: `show c004 --blocks 0-40`")
    s.add_argument("chapter")
    s.add_argument("--blocks", help="range, e.g. 0-40")
    s.add_argument("--translated", action="store_true", help="also print the translations")
    s.set_defaults(fn=cmd_show)

    s = sub.add_parser("doctor", help="check dependencies")
    s.set_defaults(fn=cmd_doctor)

    s = sub.add_parser("build", help="assemble the translated book")
    s.add_argument("--formats", help="comma list of pdf,html,epub,docx,md")
    s.add_argument("--page-size", help="A4, Letter, A5 ...")
    s.add_argument("--allow-missing", action="store_true", help="draft build with source text for gaps")
    s.add_argument("--force", action="store_true", help="build even if checks fail")
    s.set_defaults(fn=cmd_build)

    s = sub.add_parser("preview", help="render PDF pages to PNG for a visual check")
    s.add_argument("--pages", help="e.g. 1-3,10 (default: a contact sheet)")
    s.add_argument("--sheet", type=int, default=9, help="pages in the contact sheet")
    s.add_argument("--dpi", type=int, default=70)
    s.set_defaults(fn=cmd_preview)

    a = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    if a.cmd not in ("init", "doctor"):
        a.project_dir = Path(a.project_dir or os.environ.get("BOOK_PROJECT") or ".")
    a.fn(a)


if __name__ == "__main__":
    main()
