# Review rubric (fidelity, not style)

Give a reviewer the review sheets `review/NNNN.review.md` (each pair is
`SRC:` / `TGT:`), and this rubric. One reviewer per ~5 chunks keeps the
reviewer's context small enough to read every pair. Sampling misses things.

## What to look for, in order of severity

1. **WRONG SEGMENT**: the translation belongs to a different source segment.
2. **OMISSION**: a sentence, clause, list item, number, URL, inline code token,
   condition or caveat from the source is missing.
3. **MEANING**: wrong meaning, flipped negation, wrong technical claim, wrong
   number, version or unit, the wrong sense of a domain term (for example a
   build "artifact" rendered as an archaeological artefact).
4. **DEGREE**: a statement made stronger, weaker, more certain or more one-sided:
   a hedge or qualifier dropped ("perhaps", "not entirely", "somewhat"), a mixed
   judgement turned into a simple one, understatement or irony turned literal,
   or the reverse. CRITICAL when it changes what the author thinks.
5. **ADDITION**: content not in the source. Small connective words are fine.
   Explanations, "corrections" and modernisations are not, unless they are
   `NOTE:` lines and notes are enabled. Check every note for factual accuracy.
6. **IDENTIFIERS**: code, commands, parameters, file names, paths or names
   altered or translated.
7. **TERMS**: a glossary term rendered differently from the glossary, or one
   term translated two ways.
8. **STRUCTURE**: list items, table cells or headings that lost or changed
   information.

Do **not** report style, word choice or fluency unless it changes the meaning.

## Output

Write `review/NNNN.findings.md` with one line per issue:

```
<segment-id> CRITICAL|MINOR <CATEGORY> - <what is wrong> (quote the source fragment)
```

CRITICAL: wrong segment, meaning error, lost sentence, harmful addition, wrong
fact in a note. MINOR: small lost token, harmless addition, term inconsistency.

End with a line: `chunks: ... | segments read: N | critical: N | minor: N`.

## After the review

The orchestrator fixes each finding in `translations/NNNN.txt`, runs
`BT check NNNN`, and settles any terminology finding in the glossary so later
chunks inherit it.
