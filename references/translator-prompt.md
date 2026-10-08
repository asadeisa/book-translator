# Translator subagent prompt

Use this prompt verbatim for each subagent, one task file per subagent. The task
file holds the rules, style, glossary, output format and check command. The
subagent needs nothing else, and must not read other project files: that keeps
its context small and its output consistent with every other chunk.

```
Read the task file <ABSOLUTE PATH TO work/NNNN.task.txt> and do exactly what it says:
write the translation file it names, run the check command it gives, and fix every
ERROR until the check passes. Re-read every WARNING's segment against the source and
fix it if the warning is right. Read only that task file (no other project files).
Reply with the final check line and, in at most 3 bullets, any term or passage you
were unsure about.
```

## Dispatch pattern

1. `BT task next --count K` claims K chunks and prints the K task paths.
   K = 3-6 is a good batch; use 1-2 for the first batch so you can review the
   output before scaling up.
2. Start K subagents in parallel, each with the prompt above and one path.
3. When they finish:
   - `BT check all` must show 0 errors.
   - `BT glossary`: accept good proposals (`--accept-all`, or `--add` a better
     rendering). If you change a term that is already in use, re-check
     (`BT check all`); the glossary warning lists every segment to fix.
   - Read the subagents' "unsure" bullets and settle them in the glossary or
     the style (`BT set style=...`), so the next batch gets the decision.
4. Repeat from step 1. `BT status` shows what is left. Chunks that a subagent
   claimed but never finished stay "claimed"; hand them out again with
   `BT task next --reclaim`.

## Model choice

The translation quality is the model's. The pipeline only prevents loss and
drift. For literary text or a language pair the model handles weakly, use the
strongest available model for translation and a different model for review.
Lower `chunk_tokens` (for example 1500) for smaller models; set it before
translation starts.
