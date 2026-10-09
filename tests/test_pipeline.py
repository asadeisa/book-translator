"""End-to-end tests for book-translator: extract -> task -> check -> remap -> build.

    pip install pytest pymupdf python-docx pillow
    pytest tests

PDF output is tested only when a Chromium browser is available.
"""
import json
import subprocess
import sys
import zipfile
import xml.dom.minidom
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import book  # noqa: E402
import render  # noqa: E402

BT = [sys.executable, "-X", "utf8", str(SCRIPTS / "book.py")]

MD = """---
title: "The Small Garden Handbook"
author: "Jane Doe"
---

# Getting Started

A garden is a **living system**. You will need *patience* and about 6 hours of sun.
Read the [official guide](https://example.org/guide) or e-mail help@example.org.

## Tools

- A spade
- A watering can
  - with a fine rose

| Plant | Sun |
|---|---|
| Mint | Partial |

Run `soil-test --ph` first:

```bash
soil-test --ph --depth 10cm
```

# Watering

Water early in the morning. Never water leaves in full sun.
"""

TRANSLATION_FR = """@@ meta.title
Le petit manuel du jardin

@@ meta.toc
Sommaire

@@ c001.t
Premiers pas

@@ c001.b0000
Un jardin est un **système vivant**. Il vous faudra de la *patience* et environ 6 heures de soleil. Lisez le [guide officiel](https://example.org/guide) ou écrivez à help@example.org.

@@ c001.b0001
Outils

@@ c001.b0002
Une bêche

@@ c001.b0003
Un arrosoir

@@ c001.b0004
avec une pomme fine

@@ c001.b0005.r0c0
Plante

@@ c001.b0005.r0c1
Soleil

@@ c001.b0005.r1c0
Menthe

@@ c001.b0005.r1c1
Mi-ombre

@@ c001.b0006
Lancez d'abord `soil-test --ph` :

@@ c002.t
Arrosage

@@ c002.b0000
Arrosez tôt le matin. N'arrosez jamais les feuilles en plein soleil.
"""


def run(*args, cwd=None, check=True):
    r = subprocess.run(BT + [str(a) for a in args], capture_output=True, text=True,
                       encoding="utf-8", cwd=cwd)
    if check and r.returncode != 0:
        raise AssertionError(f"{args} failed:\n{r.stdout}\n{r.stderr}")
    return r


def blocks(project: Path):
    data = json.loads((project / "source" / "book.json").read_text(encoding="utf-8"))
    return data, [(c["id"], b) for c in data["chapters"] for b in c["blocks"]]


@pytest.fixture
def md_project(tmp_path):
    src = tmp_path / "garden.md"
    src.write_text(MD, encoding="utf-8")
    proj = tmp_path / "garden-fr"
    run("init", src, "--to", "fr", "-p", proj)
    return src, proj


# ---------------------------------------------------------------------------
# extraction
# ---------------------------------------------------------------------------

def test_markdown_structure(md_project):
    _, proj = md_project
    data, bl = blocks(proj)
    assert data["meta"]["title"] == "The Small Garden Handbook"
    assert [c["title"] for c in data["chapters"]] == ["Getting Started", "Watering"]
    types = [b["type"] for _, b in bl]
    assert types.count("item") == 3 and "table" in types and "code" in types
    nested = [b for _, b in bl if b["type"] == "item" and b["depth"] == 1]
    assert nested and nested[0]["text"] == "with a fine rose"
    assert json.loads((proj / "project.json").read_text())["source_lang"] == "en"


def test_html_inline_markup(tmp_path):
    src = tmp_path / "g.html"
    src.write_text('<html lang="en"><head><title>G</title></head><body><h1>One</h1>'
                   '<p>A <b>bold</b> word, <code>x = 1</code> and <a href="https://a.b">a link</a>.</p>'
                   '<pre>line 1\n  line 2</pre><h1>Two</h1><p>End.</p></body></html>', encoding="utf-8")
    run("init", src, "--to", "de", "-p", tmp_path / "p")
    _, bl = blocks(tmp_path / "p")
    para = next(b for _, b in bl if b["type"] == "para")
    assert para["text"] == "A **bold** word, `x = 1` and [a link](https://a.b)."
    code = next(b for _, b in bl if b["type"] == "code")
    assert code["text"] == "line 1\n  line 2"


def test_docx_and_txt(tmp_path):
    docx = pytest.importorskip("docx")
    d = docx.Document()
    d.add_heading("First", 1)
    p = d.add_paragraph("Plain and ")
    p.add_run("bold").bold = True
    d.add_paragraph("item", style="List Bullet")
    d.add_heading("Second", 1)
    d.add_paragraph("More text here.")
    d.save(tmp_path / "a.docx")
    run("init", tmp_path / "a.docx", "--to", "es", "-p", tmp_path / "d")
    data, bl = blocks(tmp_path / "d")
    assert [c["title"] for c in data["chapters"]] == ["First", "Second"]
    assert any(b["type"] == "item" for _, b in bl)
    assert any("**bold**" in b.get("text", "") for _, b in bl)

    (tmp_path / "b.txt").write_text("CHAPTER 1\n\nFirst para.\n\nCHAPTER 2\n\nSecond para.\n", encoding="utf-8")
    run("init", tmp_path / "b.txt", "--to", "es", "-p", tmp_path / "t")
    data, _ = blocks(tmp_path / "t")
    assert [c["title"] for c in data["chapters"]] == ["CHAPTER 1", "CHAPTER 2"]


def test_epub_spine_and_language(tmp_path):
    path = tmp_path / "b.epub"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", '<container><rootfiles><rootfile full-path="OPS/b.opf"/></rootfiles></container>')
        z.writestr("OPS/b.opf", '<package><metadata><dc:title>Le Livre</dc:title><dc:language>fr</dc:language></metadata>'
                   '<manifest><item id="a" href="a.xhtml" media-type="application/xhtml+xml"/>'
                   '<item id="b" href="b.xhtml" media-type="application/xhtml+xml"/></manifest>'
                   '<spine><itemref idref="a"/><itemref idref="b"/></spine></package>')
        z.writestr("OPS/a.xhtml", "<html><body><h1>Un</h1><p>Le <em>jardin</em> &amp; la maison.</p></body></html>")
        z.writestr("OPS/b.xhtml", "<html><body><h1>Deux</h1><p>Fin.</p></body></html>")
    run("init", path, "--to", "en", "-p", tmp_path / "e")
    data, bl = blocks(tmp_path / "e")
    assert [c["title"] for c in data["chapters"]] == ["Un", "Deux"]
    assert bl[0][1]["text"] == "Le *jardin* & la maison."
    assert json.loads((tmp_path / "e" / "project.json").read_text())["source_lang"] == "fr"


def test_pdf_outline_code_and_lists(tmp_path):
    pymupdf = pytest.importorskip("pymupdf")
    doc = pymupdf.open()
    for n in (1, 2):
        page = doc.new_page()
        page.insert_text((72, 80), f"Chapter {n}: Topic {n}", fontsize=22, fontname="hebo")
        page.insert_text((72, 130), f"This is the body paragraph of chapter {n} in a normal font.",
                         fontsize=10, fontname="helv")
        page.insert_text((72, 160), "ls -la /var/log", fontsize=10, fontname="cour")
        page.insert_text((72, 190), "• first point of the list", fontsize=10, fontname="helv")
        page.insert_text((72, 205), "• second point of the list", fontsize=10, fontname="helv")
    doc.set_toc([[1, "Chapter 1: Topic 1", 1], [1, "Chapter 2: Topic 2", 2]])
    doc.save(tmp_path / "b.pdf")
    run("init", tmp_path / "b.pdf", "--to", "it", "-p", tmp_path / "p")
    data, bl = blocks(tmp_path / "p")
    assert [c["title"] for c in data["chapters"]] == ["Chapter 1: Topic 1", "Chapter 2: Topic 2"]
    types = [b["type"] for _, b in bl]
    assert types.count("code") == 2 and types.count("item") == 4
    assert not any(b.get("type") == "heading" and "Chapter" in b.get("text", "") for _, b in bl)


# ---------------------------------------------------------------------------
# tasks and checks
# ---------------------------------------------------------------------------

def test_task_check_catches_lost_link(md_project):
    _, proj = md_project
    out = run("task", "next", "-p", proj).stdout
    assert "task 0001" in out
    task = (proj / "work" / "0001.task.txt").read_text(encoding="utf-8")
    assert "@@ c001.b0000 para" in task and "soil-test --ph --depth 10cm" in task  # code shown as context

    broken = TRANSLATION_FR.replace("[guide officiel](https://example.org/guide)", "guide officiel")
    (proj / "translations" / "0001.txt").write_text(broken, encoding="utf-8")
    r = run("check", "1", "-p", proj, check=False)
    assert r.returncode == 1 and "https://example.org/guide" in r.stdout

    (proj / "translations" / "0001.txt").write_text(TRANSLATION_FR, encoding="utf-8")
    r = run("check", "1", "-p", proj)
    assert "OK - 0 errors" in r.stdout
    assert "100% verified" in run("status", "-p", proj).stdout


def test_check_missing_and_untranslated(md_project):
    _, proj = md_project
    text = TRANSLATION_FR.replace("@@ c002.b0000\nArrosez tôt le matin. N'arrosez jamais les feuilles en plein soleil.\n", "")
    text = text.replace("Une bêche", "A spade but untranslated here")
    (proj / "translations" / "0001.txt").write_text(text, encoding="utf-8")
    r = run("check", "1", "-p", proj, check=False)
    assert "c002.b0000: missing" in r.stdout
    assert r.returncode == 1


def test_parse_translation_format():
    t = book.parse_translation("@@ a.1 para\nHello\nworld\n\n@@ a.2 =\n@@ -- code\nx\n@@ glossary\nfoo => bar\n")
    assert t["segs"] == {"a.1": "Hello\nworld"}
    assert t["keep"] == {"a.2"} and t["glossary"] == {"foo": "bar"}


def test_glossary_sense_and_code_are_ignored():
    g = {"terms": {"output (noun)": "salida", "release": "versión"}, "keep": []}
    assert book.glossary_for("The output is shown.", g) == [("output (noun)", "salida")]
    assert book.glossary_for("See `/etc/os-release` and /etc/redhat-release.", g) == []


# ---------------------------------------------------------------------------
# voice mode
# ---------------------------------------------------------------------------

def test_voice_study_brief_people_and_edit(md_project):
    _, proj = md_project
    out = run("voice", "study", "-p", proj).stdout
    study = (proj / "work" / "voice-study.task.txt").read_text(encoding="utf-8")
    assert "voice-study.task.txt" in out and "## Style profile" in study and "@@ -- c001.b0000" in study
    assert "Voice" not in (run("task", "1", "-p", proj) and
                           (proj / "work" / "0001.task.txt").read_text(encoding="utf-8"))

    # an agent wrote the brief and people: every task now carries them
    (proj / "voice" / "brief.md").write_text("- Warm, practical, second person.", encoding="utf-8")
    (proj / "voice" / "people.md").write_text("- Jane (Jeanne): woman; the gardener; plain speech\n"
                                              "- Bob: man; not in this book", encoding="utf-8")
    run("task", "1", "-p", proj)
    task = (proj / "work" / "0001.task.txt").read_text(encoding="utf-8")
    assert "10. Keep the author's voice" in task and "Warm, practical" in task
    assert "## People in this chunk" not in task      # neither name occurs in the text
    (proj / "voice" / "quotes.md").write_text("- c002.b0000: Water early in the morning.\n"
                                              "- c009.b0001: not in this book", encoding="utf-8")
    run("task", "1", "-p", proj)
    task = (proj / "work" / "0001.task.txt").read_text(encoding="utf-8")
    assert "## Quotable lines in this chunk" in task and "- c002.b0000: Water early" in task

    # edit needs a checked chunk; it keeps the draft and check compares against it
    (proj / "translations" / "0001.txt").write_text(TRANSLATION_FR, encoding="utf-8")
    assert "check 1" in run("edit", "1", "-p", proj).stdout
    run("check", "1", "-p", proj)
    out = run("edit", "next", "-p", proj).stdout
    edit = (proj / "work" / "0001.edit.txt").read_text(encoding="utf-8")
    assert (proj / "translations" / "_draft" / "0001.txt").exists()
    assert "SOURCE: Water early in the morning." in edit and "DRAFT: Arrosez tôt le matin." in edit
    assert "## Quotable lines in this chunk" in edit

    import time
    time.sleep(1.1)
    cut = TRANSLATION_FR.replace("Il vous faudra de la *patience* et environ 6 heures de soleil. ", "")
    (proj / "translations" / "0001.txt").write_text(cut, encoding="utf-8")
    r = run("check", "1", "-p", proj, check=False)
    assert "shorter than the draft" in r.stdout and "numbers in the draft but not in the edit: 6" in r.stdout
    assert "voice-edited chunks: 1 of 1" in run("status", "-p", proj).stdout


def test_arabic_lint_and_grammar_notes():
    import langs
    assert langs.lint("ar", "«قالت هي إنها سترقص»")
    assert langs.lint("ar", "«اقترب، يا العندليب»")
    assert not langs.lint("ar", "«قالت إنها سترقص»، قال العندليب. «يا الله!»")
    assert "verbal sentence" in book.lang_notes("ar") and book.lang_notes("xx") == ""
    assert langs.term_in("الحب", "فبحبّي", "ar") and langs.term_in("الحبيبة", "حبيبتي", "ar")
    assert not langs.term_in("الحب", "الكره", "ar") and not langs.term_in("Liebe", "Hass", "de")
    assert langs.lint("de", 'Sie sagte: "Nein."') and not langs.lint("de", "Sie sagte: „Nein.“")
    assert langs.lint("zh", "她说,不.") and not langs.lint("zh", "她说：“不。”")


def test_voice_profile_and_people():
    import voice
    segs = [{"id": f"c001.b{i:04d}", "kind": "para",
             "text": "You people remember youth with joy. You people call it golden. "
                     "But I remember it as a prisoner remembers his chains, and the rain, and the wind."}
            for i in range(6)]
    pr = voice.profile(segs)
    assert pr["paragraphs"] == 6 and ("You people", 12) in pr["openers"]
    assert len(voice.pick_passages(segs, 3)) == 3
    people = voice.parse_people("- سلمى كرامة (Selma Karamy): woman; quiet\n- Farris Effandi: old man\nnot a line")
    assert len(people) == 2
    assert voice.people_for("ونظرت سلمى إلى أبيها", people) == ["سلمى كرامة (Selma Karamy): woman; quiet"]


# ---------------------------------------------------------------------------
# re-extraction keeps finished work
# ---------------------------------------------------------------------------

def test_reinit_carries_translations_over(md_project):
    src, proj = md_project
    (proj / "translations" / "0001.txt").write_text(TRANSLATION_FR, encoding="utf-8")
    src.write_text(MD.replace("# Getting Started\n\n", "# Getting Started\n\nA brand new first paragraph.\n\n"),
                   encoding="utf-8")
    out = run("init", src, "--to", "fr", "-p", proj, "--force").stdout
    assert "segments to translate now: 1" in out
    r = run("check", "all", "-p", proj, check=False)
    assert r.stdout.count("missing") == 1          # only the new paragraph
    task = run("task", "1", "-p", proj).stdout
    assert "task 0001" in task
    assert "partly translated" in (proj / "work" / "0001.task.txt").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

def test_build_html_epub_md_docx(md_project):
    _, proj = md_project
    (proj / "translations" / "0001.txt").write_text(TRANSLATION_FR, encoding="utf-8")
    run("check", "1", "-p", proj)
    out = run("build", "-p", proj, "--formats", "html,epub,md,docx").stdout
    files = {Path(line.strip()).suffix for line in out.splitlines() if line.strip()}
    assert {".html", ".epub", ".md", ".docx"} <= files
    html = next((proj / "output").glob("*.html")).read_text(encoding="utf-8")
    assert '<html lang="fr" dir="ltr">' in html and "<table>" in html and "Sommaire" in html
    with zipfile.ZipFile(next((proj / "output").glob("*.epub"))) as z:
        assert z.namelist()[0] == "mimetype"
        for n in z.namelist():
            if n.endswith((".xhtml", ".opf", ".ncx")):
                xml.dom.minidom.parseString(z.read(n))


def test_rtl_document_flags(md_project):
    _, proj = md_project
    cfg = json.loads((proj / "project.json").read_text(encoding="utf-8"))
    cfg["target_lang"] = "ar"
    (proj / "project.json").write_text(json.dumps(cfg), encoding="utf-8")
    run("build", "-p", proj, "--formats", "html,docx", "--allow-missing")
    html = next((proj / "output").glob("*.html")).read_text(encoding="utf-8")
    assert 'dir="rtl"' in html
    with zipfile.ZipFile(next((proj / "output").glob("*.docx"))) as z:
        body = z.read("word/document.xml").decode("utf-8")
    assert "<w:bidi/>" in body and "<w:bidiVisual/>" in body


def test_split_rtl_keeps_latin_tokens_whole():
    parts = render.split_rtl("يدعم C# و .NET مع DECIMAL(5,2) فقط")
    latin = [t for t, rtl in parts if not rtl]
    assert latin == ["C#", ".NET", "DECIMAL(5,2)"]


def test_parse_inline():
    runs = render.parse_inline("a **b** *c* `d*e` [f](https://g) \\*h")
    assert [(r["text"].strip(), r["b"], r["i"], r["code"], r["url"]) for r in runs if r["text"].strip()] == [
        ("a", False, False, False, None), ("b", True, False, False, None), ("c", False, True, False, None),
        ("d*e", False, False, True, None), ("f", False, False, False, "https://g"), ("*h", False, False, False, None)]


@pytest.mark.skipif(not render._browser_candidates() and not pytest.importorskip("pymupdf"),
                    reason="no Chromium browser")
def test_build_pdf_with_toc(md_project):
    if not render._browser_candidates():
        try:
            import playwright  # noqa: F401
        except ImportError:
            pytest.skip("no Chromium browser and no Playwright")
    import pymupdf
    _, proj = md_project
    (proj / "translations" / "0001.txt").write_text(TRANSLATION_FR, encoding="utf-8")
    run("check", "1", "-p", proj)
    run("build", "-p", proj, "--formats", "pdf")
    doc = pymupdf.open(next((proj / "output").glob("*.pdf")))
    toc = doc.get_toc()
    assert [t[1] for t in toc if t[0] == 1] == ["Premiers pas", "Arrosage"]
    assert "Sommaire" in doc[1].get_text()
