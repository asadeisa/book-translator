# Voice mode: keeping the author's style

Use it for literature, essays, memoirs and any book where *how* it is said
matters as much as *what* is said. Technical books don't need it.

The normal pipeline keeps the meaning: nothing is dropped, terms stay
consistent. But a fluent model left alone tends to write every book in the
same smooth, modern, neutral voice. It splits long sentences, removes
repetition it sees as clumsy, explains metaphors and modernises archaic words.
Voice mode adds three things:

| Step | Who | Writes |
|---|---|---|
| 1. `BT voice study` | ONE agent, ideally with web search | `voice/study.md`, `voice/brief.md`, `voice/people.md`, motif glossary entries |
| 2. `BT task next` | translators, as usual | every task now carries the brief, rule 10 and the people in that chunk |
| 3. `BT edit next` | an editor per chunk | revises the checked draft toward the voice; the draft is kept in `translations/_draft/` |

## 1. Study (once per book, before chunk 1)

```
BT voice study            # writes work/voice-study.task.txt
```

The task holds a computed style profile (sentence and paragraph lengths,
clauses per sentence, dialogue share, repeated openings, motif candidates) and
about 8 passages spread through the book. Give it to one agent, preferably the
strongest model available and one with web search:

```
Read the task file <ABSOLUTE PATH TO work/voice-study.task.txt> and do exactly
what it says. You may use web search for the research step and the `show`
command it gives to read more of the book. Do not read other project files
directly. Reply as the task asks.
```

Then read `voice/brief.md` yourself. It is copied into every task, so it must
be short (300 words at most), concrete and correct. Fix anything vague
("keep the beautiful style") or wrong before translating. Accept or correct
the motif glossary entries with `BT glossary`.

## 2. Translate

Nothing changes in the dispatch (`references/translator-prompt.md`). While
`voice/brief.md` exists, every task file gets:

- rule 10: keep sentence length and rhythm, repetitions and parallelism,
  every image as an image, and one register throughout;
- a "Voice" section (the brief);
- "People in this chunk": the `voice/people.md` lines whose names occur in
  the chunk (gender, how they speak, formal or informal address).

## 3. Edit (per chunk, after `check` passes)

```
BT edit next --count 3    # copies each chunk to translations/_draft/, writes work/NNNN.edit.txt
```

Give each edit task to a fresh agent with the same one-line prompt as a
translation task:

```
Read the task file <ABSOLUTE PATH TO work/NNNN.edit.txt> and do exactly what it
says: overwrite the translation file it names, run the check command it gives,
and fix every ERROR. Read only that task file. Reply as the task asks.
```

The edit task shows SOURCE and DRAFT for every segment, the brief, the study,
the people and the glossary. The editor may not change meaning. After the
edit, `check` runs all the usual checks and also compares each segment with
its draft: it warns when a segment became much shorter, lost a number, or lost
sentences. `status` shows how many chunks have been voice-edited.

To undo an edit, copy `translations/_draft/NNNN.txt` back over
`translations/NNNN.txt`.

## Voice review (optional, after the edit)

Give a reviewer `BT review` sheets for a few chunks plus `voice/brief.md`, and
ask for one line per problem:

```
<segment-id> VOICE <FLATTENED|LOST-FIGURE|RHYTHM|REGISTER|REPETITION|DIALOGUE> - <what> (quote the source)
```

Fidelity findings (`references/review-rubric.md`) still come first: a
beautiful sentence that says something else is a mistranslation.

## Limits

- The study is only as good as its facts. Check the researched claims in
  `voice/study.md` before trusting them, and keep its sources.
- Wordplay, rhyme and dialect are where machine translation loses most, even
  with a good brief. Say so to the reader.
- Do not paste a published translation into the study or the brief. Name it,
  do not copy it.
