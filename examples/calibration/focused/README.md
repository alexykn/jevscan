# Focused-question fixture corpus

This is a compact, public, source-only fixture corpus for the focused JEV01,
JEV02, and JEV04 question experiment. `sources/` is the only scanned evidence
root. `MANIFEST.yaml` contains provisional mapping annotations outside that
root; they are not human ground truth and are never provider input.

Each affected rule has six reserved diagnostic groups that were predeclared as
held-out cases but never evaluated. JEV01 and JEV04 have three
provisional positive and three provisional negative groups; JEV02 has two
reviewed positive groups (expected levels 3 and 2) and four negative groups
(levels 1, 0, 0, and 0). Related variants would share one group; this corpus
uses one neutral source file per group. The fixtures cover
callback and `await` boundaries, immutable captured values, cohesive lifecycle
orchestration, interleaved responsibilities, obscure and merely local control
flow, and redundant versus boundary-justified validation.

Development imports every independently adjudicated JEV01, JEV02, and JEV04
case from `calibration/expanded-cases.jsonl`, including the reviewed
`quarry.ts` and `summit.js` snapshots. All imported cases are development-only
metadata; their stored answers are never reused for focused candidate variants.
Additional documents that are not already part of production evidence are
rejected rather than recorded as if supplied.

The experiment stopped after every development candidate failed its declared
acceptance rule, so these reserved cases were not sent to the provider. See
[`calibration/FOCUSED_QUESTIONS_RESULTS.md`](../../../calibration/FOCUSED_QUESTIONS_RESULTS.md)
for the measured development results. The one-off capture and extraction code
was removed after the experiment; this directory retains the neutral fixtures
and source-only review evidence, not a second supported calibration workflow.
