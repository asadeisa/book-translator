"""Voice mode: keep the author's style, not only the meaning.

Three steps, each a self-contained task file for one agent:

1. study  - one agent researches the author and the book, reads a style
            profile and sample passages, and writes voice/study.md,
            voice/brief.md and voice/people.md.
2. translate - every normal translation task now carries the brief and the
            people that appear in its chunk.
3. edit   - after a chunk is translated and checked, an editor compares the
            draft with the source and revises it toward the author's voice.
            The draft is kept in translations/_draft/ and `check` warns when
            an edited segment lost content.

This module only builds text; book.py owns the project files and commands.
"""
from __future__ import annotations

import re
import statistics
from collections import Counter

import langs

SENT_RE = re.compile(r"[.!?؟۔।…]+(?=[\s\"'”»)]|$)|[。！？]")
CLAUSE_RE = re.compile(r"[,،;؛:—–、，；]")
QUOTE_RE = re.compile(r"[«»“”„\"]|^\s*[—–-]\s", re.M)
WORD_RE = re.compile(r"[^\W\d_]+", re.U)

VOICE_RULE = """10. Keep the author's voice (see "Voice"). Keep sentence length and rhythm close to
   the source: do not split long sentences or merge short ones unless {tgt} grammar
   forces it. Keep repetitions, refrains and parallel structures. Keep every image and
   metaphor as an image; never explain or flatten it. Keep the register (archaic,
   formal, plain, colloquial) the same throughout. Fluent is not the same as plain."""


# ---------------------------------------------------------------------------
# style profile
# ---------------------------------------------------------------------------

def _sentences(text: str) -> list[str]:
    parts = [s.strip() for s in SENT_RE.split(text)]
    return [s for s in parts if WORD_RE.search(s)]


def profile(segs: list[dict]) -> dict:
    """Numbers that describe how the book is written (any language)."""
    paras = [s["text"] for s in segs if s["kind"] in ("para", "quote", "item")]
    words_p = [langs.word_count(t) for t in paras if t.strip()]
    sents = [x for t in paras for x in _sentences(t)]
    words_s = [langs.word_count(x) for x in sents] or [0]
    clauses = [len(CLAUSE_RE.findall(x)) + 1 for x in sents] or [1]
    text = "\n".join(paras)
    total_words = sum(words_p) or 1
    q = len(re.findall(r"[?？؟]", text))
    ex = len(re.findall(r"[!！]", text))
    dialogue = sum(1 for t in paras if QUOTE_RE.search(t))

    # repeated openers (anaphora / refrains) and repeated 3-word phrases
    def head(x, n):
        return " ".join(WORD_RE.findall(x)[:n])
    openers = Counter(head(x, 2) for x in sents if len(WORD_RE.findall(x)) >= 4)
    tri = Counter()
    for t in paras:
        w = [x.lower() for x in WORD_RE.findall(t)]
        tri.update(" ".join(w[i:i + 3]) for i in range(len(w) - 2))
    # motif candidates: frequent longer words. Function words are spread over most
    # paragraphs, motifs are not, so words found in over a third of them are skipped.
    spread = Counter(w for t in paras for w in {x.lower() for x in WORD_RE.findall(t)})
    common = {w for w, k in spread.items() if k > max(3, len(paras) / 3)}
    vocab = Counter(w.lower() for w in WORD_RE.findall(text)
                    if len(w) >= 4 and w.lower() not in common)

    def pct(xs, q):
        xs = sorted(xs)
        return xs[min(len(xs) - 1, int(len(xs) * q))] if xs else 0
    return {
        "paragraphs": len(paras),
        "words": total_words,
        "words_per_paragraph": (statistics.median(words_p) if words_p else 0, max(words_p or [0])),
        "words_per_sentence": (statistics.median(words_s), pct(words_s, 0.9), max(words_s)),
        "clauses_per_sentence": round(statistics.mean(clauses), 1),
        "questions_per_1000_words": round(1000 * q / total_words, 1),
        "exclamations_per_1000_words": round(1000 * ex / total_words, 1),
        "dialogue_paragraphs_pct": round(100 * dialogue / max(len(paras), 1)),
        "openers": [(k, v) for k, v in openers.most_common(12) if v >= 3],
        "phrases": [(k, v) for k, v in tri.most_common(15) if v >= 3],
        "frequent_words": [(k, v) for k, v in vocab.most_common(30) if v >= 3],
    }


def profile_text(pr: dict) -> str:
    wp, ws = pr["words_per_paragraph"], pr["words_per_sentence"]
    lines = [
        f"- {pr['paragraphs']} paragraphs, about {pr['words']:,} words",
        f"- words per paragraph: median {wp[0]:.0f}, longest {wp[1]}",
        f"- words per sentence: median {ws[0]:.0f}, 90th percentile {ws[1]}, longest {ws[2]}",
        f"- clauses per sentence (commas, semicolons, dashes + 1): {pr['clauses_per_sentence']}",
        f"- questions per 1000 words: {pr['questions_per_1000_words']}; "
        f"exclamations: {pr['exclamations_per_1000_words']}",
        f"- paragraphs with dialogue or quotation marks: {pr['dialogue_paragraphs_pct']}%",
    ]
    if pr["openers"]:
        lines.append("- repeated openings (anaphora, refrains): "
                      + "; ".join(f"\"{k}\" x{v}" for k, v in pr["openers"]))
    if pr["phrases"]:
        lines.append("- repeated 3-word phrases: " + "; ".join(f"\"{k}\" x{v}" for k, v in pr["phrases"]))
    if pr["frequent_words"]:
        lines.append("- frequent longer words (motif candidates): "
                     + ", ".join(f"{k} x{v}" for k, v in pr["frequent_words"]))
    return "\n".join(lines)


def pick_passages(segs: list[dict], count: int = 8, max_words: int = 220) -> list[dict]:
    """Passages spread over the book, plus the longest and the most dialogue-like."""
    paras = [s for s in segs if s["kind"] in ("para", "quote") and langs.word_count(s["text"]) >= 25]
    if not paras:
        return []
    picked = []

    def add(s):
        if s not in picked:
            picked.append(s)
    add(max(paras, key=lambda s: langs.word_count(s["text"])))
    dia = [s for s in paras if QUOTE_RE.search(s["text"])]
    if dia:
        add(max(dia, key=lambda s: len(QUOTE_RE.findall(s["text"]))))
    # the rest evenly spread from the first paragraph to the last
    spots = [round(k * (len(paras) - 1) / max(1, count - 1)) for k in range(count)]
    for i in spots + list(range(len(paras))):
        if len(picked) >= count:
            break
        add(paras[i])
    picked.sort(key=lambda s: paras.index(s))
    out = []
    for s in picked:
        words = s["text"].split()
        text = " ".join(words[:max_words]) + (" ..." if len(words) > max_words else "")
        out.append({"id": s["id"], "text": text})
    return out


# ---------------------------------------------------------------------------
# task texts
# ---------------------------------------------------------------------------

def study_task(meta: dict, src: dict, tgt: dict, pr: dict, passages: list[dict],
               chapters: list[tuple[str, str]], voice_dir, script, project) -> str:
    author = meta.get("author") or "(author unknown - find out)"
    head = [
        "# book-translator: voice study",
        f"Book: {meta.get('title', '')}",
        f"Author: {author}",
        f"Translation: {src['name']} ({src['code']}) -> {tgt['name']} ({tgt['code']})",
        "",
        "Your job is to work out HOW this book is written, so that every translator can keep",
        f"the author's voice in {tgt['name']}. You do not translate the book itself.",
        "",
        "## Step 1: research (if you have web search or web fetch tools)",
        "Look up the author and this book: when and where it was written, the literary",
        "tradition or movement, what in the author's life matters for this book, how critics",
        "describe the style, its main themes, and existing translations into "
        f"{tgt['name']} and how they were received. Write down each fact with its source.",
        "State only facts you found or are certain of, and mark anything else \"unverified\".",
        "If you cannot search, say so in the study and work from the text alone.",
        "Never copy text from a published translation (copyright); you may name it.",
        "",
        "## Step 2: read",
        "The style profile and the passages below. To read more of the book:",
        f"    python \"{script}\" show <chapter-id> --blocks 0-30 --full --project \"{project}\"",
        "Read a little from several chapters rather than one chapter in full.",
        "Chapters: " + "; ".join(f"{cid} {title}" for cid, title in chapters[:60]),
        "",
        f"## Step 3: write {voice_dir / 'study.md'}",
        "Sections, in this order:",
        "1. Facts: author, date, place, genre, tradition; what the book is about; narrator and",
        "   point of view. Sources for the researched facts.",
        "2. Voice: register and diction (archaic, formal, plain, colloquial, dialect);",
        "   sentence architecture (long periodic sentences, parallelism, anaphora, refrains,",
        "   lists); rhythm and sound (rhymed prose, alliteration, cadence); fields of imagery;",
        "   rhetorical devices; humour or irony; emotional temperature. Back every claim with",
        "   a short quote from the source (at most 15 words) and its segment id.",
        "3. Motifs and key words: recurring words and images that must stay recognisable",
        f"   across the book, each with one proposed {tgt['name']} rendering.",
        "4. People: every named character with gender, role, how they speak and how they",
        "   address others (formal or informal), and how to write the name.",
        f"5. Pitfalls: what a fluent but careless {tgt['name']} translation would destroy in",
        "   this book (flattened metaphors, broken-up long sentences, normalised repetition,",
        "   modernised register, explained allusions, wrong gender ...), each with an example.",
        f"6. Strategy: which {tgt['name']} register or literary tradition best echoes this voice,",
        "   and how to handle sentence length, repetition, imagery, wordplay, dialect, cultural",
        "   references and words with no equivalent.",
        f"7. Model passages: translate 3 of the passages below into {tgt['name']} as examples",
        "   of the voice, each labelled with its segment id.",
        "",
        f"## Step 4: write {voice_dir / 'brief.md'} (at most 300 words)",
        "This text is copied into every translation task, so it must stand alone: the voice in",
        "3-5 bullets, then a short DO / DON'T list (repetition, sentence length, imagery,",
        "register, dialogue). No facts about the author unless they change a translation choice.",
        "",
        f"## Step 5: write {voice_dir / 'people.md'}",
        "One line per person, starting with the name as written in the source:",
        "- <name in source> (<name in target>): <gender>; <role>; <how they speak>; <form of address>",
        "",
        "## Step 6: glossary",
        "For each motif from section 3, run:",
        f"    python \"{script}\" glossary --add \"<source word>=<rendering>\" --project \"{project}\"",
        "",
        "Reply with 5 bullets that sum up the voice, and the list of files you wrote.",
        "",
        "## Style profile (computed from the source)",
        profile_text(pr),
        "",
        "## Passages",
    ]
    for ps in passages:
        head += [f"@@ -- {ps['id']}", ps["text"], ""]
    return "\n".join(head) + "\n"


def parse_people(text: str) -> list[tuple[list[str], str]]:
    """Lines '- Name (Target): note' -> [(name words, line)]."""
    out = []
    for line in text.splitlines():
        m = re.match(r"\s*[-*]\s*([^:]+):\s*(.+)", line)
        if not m:
            continue
        name = re.sub(r"\([^)]*\)", " ", m.group(1))
        words = [w for w in WORD_RE.findall(name) if len(w) >= 3]
        if words:
            out.append((words, line.strip().lstrip("-* ").strip()))
    return out


def people_for(text: str, people: list[tuple[list[str], str]], limit: int = 12) -> list[str]:
    hits = []
    for words, line in people:
        if any(re.search(rf"(?<!\w){re.escape(w)}", text) for w in words):
            hits.append(line)
    return hits[:limit]


EDIT_RULES = """1. Meaning is fixed. Never add, drop or change information, names, numbers or
   negations. Every sentence of the source must still be in the translation.
2. For each segment, read SOURCE, then DRAFT, and revise the draft toward the author's
   voice: restore images and metaphors the draft flattened or explained; restore
   repetitions, refrains and parallel structures it normalised; bring sentence length
   and rhythm back toward the source; fix register slips (modern idiom in an old text,
   stiff formality in a plain one); make each character sound like the People notes.
3. Keep the markup (**bold**, *italic*, `code`, [text](url)), every verbatim token, and
   the glossary renderings exactly as in the draft.
4. Leave a segment unchanged when it already works. Do not rewrite for the sake of it,
   and do not "improve" the author: if the source is plain, stay plain.
5. Use "@@ <id> =" only where the draft already did."""


def edit_task(n: int, total: int, meta: dict, src: dict, tgt: dict, study: str, brief: str,
              people: list[str], gloss: list[tuple[str, str]], items: list[tuple[str, str, str, str]],
              out_path, draft_path, script, project) -> str:
    head = [
        f"# book-translator: voice edit, chunk {n:04d} of {total:04d}",
        f"Book: {meta.get('title', '')}" + (f" by {meta['author']}" if meta.get("author") else ""),
        f"{src['name']} -> {tgt['name']}",
        "",
        "You are the style editor. The draft below is complete and has passed the meaning",
        f"checks. Make it read as if the author had written it in {tgt['name']}.",
        "",
        "## Editing rules",
        EDIT_RULES,
        "",
        "## Voice brief",
        brief.strip() or "(no brief)",
    ]
    if study.strip():
        head += ["", "## Voice study (reference)", study.strip()]
    if people:
        head += ["", "## People in this chunk"] + [f"- {x}" for x in people]
    if gloss:
        head += ["", "## Glossary (keep these renderings)"] + [f"{k} => {v}" for k, v in gloss]
    head += [
        "",
        "## Output",
        f"Overwrite {out_path} with the full chunk in the same format as the draft:",
        "    @@ <segment-id>",
        "    <edited or unchanged translation>",
        "Every segment id below must appear exactly once. Do not write the SOURCE lines.",
        f"The draft is saved at {draft_path}. When the file is written, run:",
        f"    python \"{script}\" check {n} --project \"{project}\"",
        "Fix every ERROR. A warning \"shorter than the draft\" means the edit may have",
        "dropped content: compare with SOURCE and restore it.",
        "Reply with the number of segments you changed and 3 short examples",
        "(draft -> edited) that show the kind of change.",
        "",
        "## Segments",
    ]
    body = []
    for sid, kind, s, d in items:
        body.append(f"@@ {sid} {kind}\nSOURCE: {s}\nDRAFT: {d}")
    return "\n".join(head) + "\n" + "\n\n".join(body) + "\n"


def drift(source: str, draft: str, edited: str, numbers) -> list[str]:
    """Warnings when an edit lost content that the draft had."""
    out = []
    if len(draft) >= 60 and len(edited) < 0.75 * len(draft):
        out.append(f"much shorter than the draft ({len(edited)} vs {len(draft)} chars) - "
                   "content dropped in editing?")
    lost = numbers(draft) - numbers(edited)
    if lost:
        out.append("numbers in the draft but not in the edit: " + ", ".join(sorted(lost)))
    nd, ne = len(_sentences(draft)), len(_sentences(edited))
    if nd >= 3 and ne < nd * 0.6:
        out.append(f"{nd} sentences in the draft, {ne} after editing")
    return out
