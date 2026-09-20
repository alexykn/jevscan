# Semantic calibration evidence

This directory records calibration inputs and experiments, not a replacement
implementation of rule assessment. Replay must use the production assessment
path documented in `docs/SEMANTIC_CALIBRATION.md`.

The completed warning-default selection, held-out tradeoffs, and evidence
limitations are in [FINAL_RESULTS.md](FINAL_RESULTS.md), with machine-readable
aggregates in `FINAL_RESULTS.json`. Earlier result pages are historical stages,
not the final installed policy.

For current project calibration, use `jevscan --calibration-output`,
`jevscan-calibration-import`, and `jevscan-calibrate --select --apply`, as
described in the [portable skill](../skills/jevscan-calibration/SKILL.md).
The scripts in this directory are repository-only research and historical
reproduction tools, not a second supported product workflow. Historical
question and error-policy experiments do not define the current selector's
warning-only contract. They are intentionally absent from Python distributions.

`public-development.jsonl` is a deliberately public reference containing
snapshots of this public jevscan repository at its recorded commit. Source
snapshots are necessary to reproduce its evidence checks. This is not a
publication convention for user captures: external-project source, manifests,
and cases remain in ignored `.jevscan-calibration/` storage.
`expanded-cases.jsonl` similarly publishes only the synthetic source corpus
and its captured judgments; labels and review corrections are recorded in
`expanded-adjudications.json`.

## Existing adjudications

`adjudications.json` preserves the independent source-only adjudication of 58
previously `Partial` findings at the recorded jevscan commit:

| Rule | Cases | Exact positive | Exact negative |
| --- | ---: | ---: | ---: |
| JEV01 | 10 | 2 | 8 |
| JEV02 | 48 | 4 | 44 |

The JEV02 level counts are 1 at level 0, 43 at level 1, 4 at level 2, and
none at level 3. Original `Partial` labels remain part of the record; exact
adjudications supplement them rather than silently changing the original
review. Match records by commit, path, and qualified symbol, not line number
in the current checkout.

For JEV01, `warning_worthy` can refer to a related rule. It is not a substitute
for `exact_claim`. In particular, the JEV01 reviewer suggested a related
control-flow warning for `_produce`, whereas the direct JEV02 reviewer assigned
level 1. The explicit cross-review disagreement is retained; direct rule
adjudication is used when measuring that rule.

## Experiment constraints

- Preserve current rule meanings. Adding rules or changing the division of
  responsibilities between built-in rules belongs in a separate PR.
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
Completed supplementary evidence includes the
[lookup-daemon evaluation](LOOKUP_RESULTS.md) and the
[baseline-only real-project assembly](BASELINE_ASSEMBLY.md).

## Round-2 JEV04 pair experiment

The predeclared round-2 plan is
[FOCUSED_QUESTIONS_PLAN.md](FOCUSED_QUESTIONS_PLAN.md). It adds
`jev04-pair-joint` and `jev04-pair-decomposed` to the existing
`focused_experiment.py` pipeline without changing packaged production rules or
calling a provider during planning and replay.

Use repeatable `--candidate` filters on `plan` and `capture` to purchase only
selected pair candidates. Held-out planning and capture still require the
development freeze and reject candidates not frozen. A pair question is paid
only for a target with exactly one bounded extractor pair whose operation and
intervening spans are covered by the unchanged requested evidence. Zero,
multiple, capped, unsupported, ambiguous, and incomplete cases are recorded as
explicit whole-target fallbacks with reasons; they are not silently counted as
clean pair judgments.

Pair-level replay combines eligible pair records with already captured
`jev04-baseline` whole-target records for ineligible parents. The output
reports fallback coverage and remains incomplete when required baseline
fallback records are absent. Decomposed pair children must carry matching pair
IDs and question metadata before their diagnostic conservative AND is
calculated; probabilities are never combined.
