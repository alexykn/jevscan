# Historical automatic selector development evaluation

The completed selection and applied warning defaults are documented in
[FINAL_RESULTS.md](FINAL_RESULTS.md). The runs below preceded that final fit.

This is a development evaluation of the calibration algorithm, not a claim
that every built-in threshold is optimal. No packaged defaults were changed.
All selection runs below are offline replays of previously captured answers.

## Objective corrections found with real data

Unit tests and code review found implementation defects, but dataset runs also
exposed two objective defects:

1. Optimizing warning and error thresholds with labels that do not distinguish
   severity allowed the selector to lower the JEV02 error threshold from 2.0
   to 1.05. Production selection now preserves error thresholds.
2. Rewarding confirmed and tentative Partial findings equally caused the
   selector to surface two ambiguous synthetic JEV01 cases as confirmed
   findings without improving any Agree or Disagree outcome. The default
   Partial utilities are now confirmed `-1`, tentative `+1`, absent `0`.
3. The public reference originally lacked explicit independence groups.
   Its descriptive `provenance.source` named a review transcript, not a source
   owner. Treating it as a group weighted the objective by reviewer. The
   importer now assigns 21 conservative frozen-file groups to the 128 cases.
   Earlier objective-weighted recommendations must be recomputed; original
   labels, hashes, and displayed answer/status comparisons are unchanged.

These changes were made using development evidence. The synthetic corpus
therefore is not an untouched final acceptance set.

## All-rule synthetic evaluation

The evaluated source snapshot is:

```text
6393505d40cd945c81a27481ebbbda894f19338cef864f7ce138b748ec11568b
```

The corpus has 32 independently reviewed mapped scenarios: 29 provider
answers and three production applicability skips. Cases span all nine
built-in rules; labels and their limitations are recorded separately in
`synthetic-adjudications.json`. The model was `jev-1.13.0`.

With the corrected warning-only objective, the selector retained the baseline
for **all nine rules**. This is a valid selection result, not evidence that
the algorithm improved accuracy. In particular, the JEV06 missed positive
remains missed; threshold changes cannot necessarily repair the model's
selected choice.

| Rule | Agree cases | Partial cases | Disagree cases | Baseline retained |
| --- | ---: | ---: | ---: | :---: |
| JEV01 | 1 | 2 | 1 | yes |
| JEV02 | 1 | 0 | 1 | yes |
| JEV03 | 1 | 0 | 1 | yes |
| JEV04 | 1 | 0 | 2 | yes |
| JEV05 | 2 | 2 | 3 | yes |
| JEV06 | 2 | 0 | 1 | yes |
| JEV07 | 1 | 0 | 1 | yes |
| JEV08 | 1 | 0 | 2 | yes |
| JEV09 | 1 | 0 | 2 | yes |

Missing-context judgments remain Partial. Applicability skips are excluded
from this table, not counted as correct negatives.

## Group sensitivity

The 29 evaluated cases occupy 17 scenario groups. A diagnostic
leave-one-group-out run kept corrected pairs and translations together.
No fold selected a changed policy.

Several folds had no development cases or lacked positive/negative support
after removing a group. In those situations, the selector retained the
baseline explicitly rather than fitting an unsupported threshold. One JEV06
fold had no candidate that surfaced its remaining positive.

This exercise checks grouping and conservative fallback behavior. It does
**not** provide strong held-out accuracy estimates: several rules have only
one independent positive scenario group.

## Public reference and sensitivity

The corrected 128-case reference uses 21 explicit frozen-file groups.
Using original labels and the default candidate budget, warning selection
produced:

| Rule | Selected warning threshold | Selected warning confidence |
| --- | --- | --- |
| JEV01 | probability 0.73 | not applicable |
| JEV02 | score 1.09 | 0.70 |
| JEV03 | probability 0.58 | not applicable |
| JEV09 | baseline retained: insufficient labeled support | not applicable |

Error thresholds remained unchanged. Changing the confirmed-Disagree penalty
from the default -8 to -4 or -12 did not change these recommendations.
Reducing the candidate budget from 4096 to 128 changed JEV02's recommendation
to score 1.20 and confidence 0.60; the other recommendations were unchanged.
This demonstrates budget sensitivity, not a guarantee that every objective
or sample perturbation is harmless.

A separate exact-adjudicated view supplements, rather than overwrites, the
original Partial labels:

- JEV01: 6 Agree / 25 Disagree. Selected probability 0.71 surfaces 5 of 6
  positives and no negatives, versus the baseline's 6 positives and 25
  negative signals.
- JEV02: 18 Agree / 59 Disagree. Selected score 1.52 / confidence 0.70
  surfaces 15 of 18 positives and 9 negative tentative findings, versus the
  baseline's 18 positives and 59 negative signals.

These are development tradeoffs. Neither view is an untouched held-out
evaluation, and no recommendation in this document was applied to packaged
defaults.

## Workflow verification

- Real `evaluate_file` and enrichment tests verify final answer/evidence
  capture, cached reruns, ordinary-report privacy, and disposition replay.
- Capture/import tests cover canonical routing and threshold tampering,
  completion metadata, and integrity checks.
- A separate temporary-project CLI test drives scan, label import, automatic
  selection, and actual supplied-YAML write-back using mocked transport.
  It changes the warning threshold while preserving the error threshold,
  comments, and unrelated configuration. It tests workflow correctness,
  not semantic model accuracy.
- The selector refactor was checked against the 128-case development results:
  selected policies, objective values, label counts, and outcome counts match.

Remaining evidence limitations include the small synthetic group counts.
The authorized lookup-daemon evaluation is now complete: see
`LOOKUP_RESULTS.md` for its 38 baseline cases and the held-out positive misses.
The larger independent synthetic evaluation follows `FINAL_EVALUATION_PLAN.md`.

The public examples make every rule testable. They do not eliminate the
remaining limits of sample size, synthetic generation, or agent adjudication.
