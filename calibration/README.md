# Semantic calibration evidence

This directory records calibration inputs and experiments, not a replacement
implementation of rule assessment. Replay must use the production assessment
path documented in `docs/SEMANTIC_CALIBRATION.md`.

The final new-rule production decision is in
[NEW_RULE_DISCOVERY.md](NEW_RULE_DISCOVERY.md), with its public aggregate in
`NEW_RULE_DISCOVERY.json`. It promotes exactly JEV12–JEV16 as non-blocking
code review signals. The existing warning-default selection and held-out
tradeoffs remain in [FINAL_RESULTS.md](FINAL_RESULTS.md), with machine-readable
aggregates in `FINAL_RESULTS.json`; earlier result pages are historical stages.

The finalist assembly links its frozen usefulness configuration, aggregate
usefulness review, and public cost ledger from
[new-rule-finalists/README.md](new-rule-finalists/README.md). Its questions
matched the original JEV12–JEV16 production contracts. The current release
retains JEV01–JEV02 wording and compacts JEV03–JEV16; outcome labels and
reporting thresholds are unchanged. Prompt version 6 stores the rule questions
needed by each evidence/target-scope group in shared state and refers to them
from short per-target questions. Scoped registries keep unrelated rubrics out
of file-wide requests; they change effective state/cache identity without a
new prompt version. Historical version-4/version-5 cases remain replayable, but are
non-comparable to version 6. Historical accuracy and usefulness measurements
are not validation of the compact wording or shared-rubric format. The bounded
[prompt-v6 evaluation](COMPACT_WIRE_VALIDATION.md) reports fresh synthetic
case outcomes, cost, and remaining support limits; its nominal held-out
sources informed the JEV01–02 wording decision and do not establish independent
accuracy equivalence. The focused experiment may
reuse version-5 records only for their source and adjudicated label when making a
new capture; it discards their stored provider answers. The five advisory rules
still set `report.blocks_exit: false`, so confirmed findings remain visible
without changing the CLI exit code.

The offline cache-runner importer is intentionally frozen at prompt version 5;
it validates that wire and rejects shared-state version-6 material instead of
stamping historical evidence with a new prompt identity.

For current project calibration, use `jevscan --calibration-output`,
`jevscan-calibration-import`, and `jevscan-calibrate --select --apply`, as
described in the [portable skill](../skills/jevscan-calibration/SKILL.md).
The scripts in this directory are repository-only research and historical
reproduction tools, not a second supported product workflow. Historical
question and error-policy experiments do not define the current selector's
warning-only contract. They are intentionally absent from Python distributions.

`public-development.jsonl` is a deliberately public reference containing the
synthetic development corpus and its captured judgments. It does not publish
external-project source, manifests, local roots, revision identifiers, or
source/target digests. Those inputs remain in ignored `.jevscan-calibration/`
storage.
`expanded-cases.jsonl` similarly publishes only the synthetic source corpus
and its captured judgments; labels and review corrections are recorded in
`expanded-adjudications.json`.

## Existing adjudications

`adjudications.json` preserves the independent source-only adjudication of 58
previously `Partial` findings:

| Rule | Cases | Exact positive | Exact negative |
| --- | ---: | ---: | ---: |
| JEV01 | 10 | 2 | 8 |
| JEV02 | 48 | 4 | 44 |

The JEV02 level counts are 1 at level 0, 43 at level 1, 4 at level 2, and
none at level 3. Original `Partial` labels remain part of the record; exact
adjudications supplement them rather than silently changing the original
review. Public records use synthetic case identity rather than machine-local
paths or source locators.

For JEV01, `warning_worthy` can refer to a related rule. It is not a substitute
for `exact_claim`. In particular, the JEV01 reviewer suggested a related
control-flow warning for `_produce`, whereas the direct JEV02 reviewer assigned
level 1. The explicit cross-review disagreement is retained; direct rule
adjudication is used when measuring that rule.

## Experiment constraints and housekeeping

- Preserve current rule meanings. The current wave's five approved additions are
  recorded in `NEW_RULE_DISCOVERY.md`; future additions or changes to the
  division of responsibilities require a separately documented wave.
- Evaluate confirmed findings and tentative findings separately. Prefer
  precise confirmed warnings while retaining useful borderline findings for
  review. Precision/recall targets are benchmarks, not absolute gates.
- Use development labels for candidate selection, freeze the candidate, and
  only then evaluate held-out groups. Do not split related implementations
  from the same owner between development and held-out sets.
- Compare candidates on identical evidence and record exact source, question,
  prompt, and model identities. Never put adjudicated labels in model input.
- Pin live experiments to `jev-1.13.0`. The focused-question capture path is
  capped cumulatively at USD 0.10, using USD 0.042 per million input tokens and
  free output tokens; other historical utilities retain their own explicit
  limits. Account for all attempts, including unsuccessful requests and
  retries.
- Keep private source, raw private responses, and private manifests outside
  committed artifacts. Public summaries may report aggregate results without
  disclosing private source.

Infrastructure tests establish replay correctness, not calibration quality.
Any recommendation to change reporting defaults must cite measured results
and disclose small samples, unresolved disagreements, and missed positives.

The final selection procedure is fixed in [FINAL_EVALUATION_PLAN.md](FINAL_EVALUATION_PLAN.md).
Completed supplementary evidence includes the synthetic lookup evaluation and
the baseline-only aggregate assembly. The current public finalist evidence is
linked from `NEW_RULE_DISCOVERY.md`; external or local-only review material is
not linked, copied, or exposed here.

## Focused-question experiment

The predeclared plan is
[FOCUSED_QUESTIONS_PLAN.md](FOCUSED_QUESTIONS_PLAN.md). It compared
whole-target and pair-bound candidates without changing packaged production
rules.

The completed development results are in
[FOCUSED_QUESTIONS_RESULTS.md](FOCUSED_QUESTIONS_RESULTS.md), with
machine-readable aggregates in `FOCUSED_QUESTIONS_RESULTS.json`. No candidate
met the predeclared acceptance rules, so packaged rules remain unchanged and
the held-out split was not executed.

The one-off live runner (`focused_experiment.py`) and candidate extractor
(`validation_candidates.py`) remain retained repository research tools after the
negative result. They are not a second supported calibration workflow, and no
broad cleanup is implied. The public fixtures, source-only adjudications,
predeclared contracts, and aggregate results remain as evidence for future
experiments.
