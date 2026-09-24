# Final built-in warning calibration

The automatic selector's frozen results are applied to the packaged defaults.
Seven warning policies changed; JEV05 and JEV07 retain their baselines.
Questions, rule meanings, applicability, and all error thresholds are unchanged.
No threshold was manually substituted after examining held-out answers.

## Selected warning policies

| Rule | Previous warning gate | Selected warning gate |
| --- | --- | --- |
| JEV01 | probability 0.50 | probability 0.71 |
| JEV02 | score 1.00, confidence 0.60 | score 1.52, confidence 0.70 |
| JEV03 | probability 0.50 | probability 0.56 |
| JEV04 | probability 0.60, confidence 0.50 | probability 0.60, confidence 0.45 |
| JEV05 | probability 0.60, confidence 0.50 | unchanged |
| JEV06 | probability 0.60, confidence 0.50 | probability approximately 0.46, confidence 0.29 |
| JEV07 | probability 0.50 | unchanged |
| JEV08 | probability 0.50 | probability 0.47 |
| JEV09 | probability 0.50 | probability 0.73 |

JEV06 stores `0.45999999999999996`, the exact observed floating-point boundary
selected by the algorithm. Rounding it to `0.46` would change the gate for that
observation. JEV02 records 2,060 generated candidates, a configured limit of
4,096, and `truncated: true`: separately bounded coordinate and pair streams
stopped before exhaustive enumeration. Its result is the best evaluated
candidate under the stated objective, not a claim of global or product optimum.

The pre-heldout selected-YAML SHA-256 was:

```text
omitted digest
```

The final CLI `--select --apply` run reproduced that exact selected YAML.
A semantic comparison against the saved baseline confirmed that only warning
gates changed. Original YAML formatting was restored separately for review.

## Evidence and procedure

- 289 development cases informed selection: the public jevscan reference with
  direct rule adjudications, baseline-question real-project cases, the original
  synthetic corpus, lookup development cases, and 72 expanded synthetic cases.
- The expansion contains 108 cases in 52 reviewed groups: 34 development and
  18 held-out groups. Repaired and translated examples stay together. Related
  JEV01 development translations were consolidated instead of inflating support.
- Source-only reviewers did not see provider answers or generator labels.
  Parent corrections and superseded judgments are retained in
  `expanded-adjudications.json`; these are agent judgments, not human ground truth.
- The objective, grouping procedure, positive-signal safeguard, baseline tie
  preference, and candidate budget were fixed before expanded measurement.
- That safeguard required only one positive signal. It was not a recall floor.
  The utility is an explicit precision/recall product policy, not a discovered
  definition of accuracy.
- The 36 expanded held-out cases were captured only after policy freeze.
  Another 44 previously inspected real-project/lookup cases are reported
  separately as supplementary held-out diagnostics.
- Model: `jev-1.13.0`. Task-wide reserved live cost: **$0.020044794 of $1.00**.
- The completed fit is compatible under the new strict selection policy:
  all 289 development cases use returned model `jev-1.13.0`, prompt version 5,
  and one prompt policy. Requested aliases are not used as model identity.

Expanded source snapshot:

```text
omitted digest
```

These experiments captured initial production Planner/ContextBuilder judgments,
not final enriched scans. The installed final-scan capture/import workflow is
verified separately by integration tests. Do not equate those two evidence scopes.

## Expanded held-out results

Counts below are **confirmed / tentative / absent**. Each rule has four cases,
from two held-out groups. Applicability skips are not counted as negatives.

| Rule | Agree: baseline → selected | Disagree: baseline → selected |
| --- | --- | --- |
| JEV01 | 1/0/0 → 0/0/1 | 1/0/2 → 0/0/3 |
| JEV02 | 1/1/0 → 0/2/0 | 0/0/2 → 0/0/2 |
| JEV03 | 1/0/1 → 1/0/1 | 0/0/2 → 0/0/2 |
| JEV04 | 0/0/1 → 0/0/1 | 0/1/2 → 1/0/2 |
| JEV05 | 0/0/1 → 0/0/1 | 0/0/3 → 0/0/3 |
| JEV06 | 0/0/1 → 1/0/0 | 0/0/3 → 0/0/3 |
| JEV07 | 1/0/0 → 1/0/0 | 1/0/2 → 1/0/2 |
| JEV08 | 0/0/1 → 0/0/1 | 0/0/2 → 0/0/2 |
| JEV09 | 2/0/0 → 2/0/0 | 0/0/2 → 0/0/2 |

JEV08's additional Partial case remains tentative under both policies.

The results are mixed, not an across-the-board accuracy improvement:

- JEV06 recovers a missed positive without adding a negative signal.
- JEV01 removes a false confirmed finding but also loses its held-out positive.
- JEV02 keeps both positives visible, but as tentative findings.
- JEV04 promotes a negative tentative finding to confirmed and still misses
  its positive.
- JEV03, JEV05, and JEV08 retain positive misses; JEV07 retains a false finding.

Across this small set, positive signals remain 7/12, confirmed positives change
from 6 to 5, and confirmed negatives remain 2. These counts do not establish
population accuracy. Per the fixed plan, valid tradeoffs were reported rather
than used to hand-pick replacement thresholds.

The product owner accepted this measured precision/recall tradeoff for the
packaged defaults. No runner-up was selected after looking at held-out data.
The evidence does not claim a universal improvement in model discrimination.
For the next acceptance experiment,
`calibration/agent-review-objective.yaml` predeclares at least three positive
and three negative support groups plus 0.80 group-normalized review-list
recall. It does not retroactively alter this completed run.

## Development and supplementary results

The largest development changes are precision/recall tradeoffs:

- JEV01 negative signals fall from 35 to 1, while positive signals fall from
  10 to 5.
- JEV02 negative signals fall from 73 to 11, while positive signals fall from
  26 to 21.
- JEV04 promotes its remaining tentative positive; JEV06 recovers two positives.
- JEV03 and JEV09 remove negative tentative signals; JEV08 recovers a tentative
  positive. JEV05 and JEV07 are unchanged.

For JEV01, confirmed precision changes from 7/22 (31.8%) to 5/6 (83.3%),
while confirmed positive recall changes from 7/10 to 5/10 and review-list
positive recall from 10/10 to 5/10. For JEV02, confirmed precision changes
from 16/32 (50.0%) to 12/13 (92.3%), confirmed positive recall from 16/26 to
12/26, and review-list positive recall from 26/26 to 21/26. This is a large
noise reduction and a material recall loss.

In the separate supplementary set, JEV01 retains all four positives while
removing four negative signals. JEV02 removes two negative signals but reduces
positive signals from 13/14 to 9/14. Those previously inspected cases are not a
fresh acceptance set.

## Why threshold selection cannot repair every failure

Several held-out answers are misranked for the exact event the rule cares
about. For JEV04, the negative `quarry.ts` case receives defect probability
0.65 and confidence 0.48, while the positive `summit.js` case receives 0.50
and 0.24. No policy requiring minimum probability and confidence can admit the
positive while rejecting that negative. Lowering JEV04 confidence promoted the
negative and still missed the positive.

The JEV01 positive in `flint.ts` receives Noul 0.69 and disappears only because
the selected threshold is 0.71. The JEV02 positive in `raven.rs` has score 1.66,
confidence 0.64, and 0.70 total probability on levels 2–3; the selected policy
keeps it tentative through the 0.70 confidence gate. These are reporting
tradeoffs, not improvements to Jev's underlying distinctions.

Cases with this ordering belong in a separate question/evidence-design
investigation. Increasing threshold-search effort cannot reverse their scores.

`FINAL_RESULTS.json` contains exact per-rule policies, hashes, objective values,
support counts, bounded-search metadata, and outcome counts. Private source
cases and receipts remain ignored. `expanded-cases.jsonl` deliberately contains
only the public synthetic source snapshots and provider answers for replay.

## Reproduction and scope

From a repository checkout, replay the public expansion without credentials:

```console
uv run jevscan-calibrate calibration/expanded-cases.jsonl --output replay.json
```

That reproduces recorded baseline judgments. To compare the published policies,
build a report-override document from the `selected_policy` values in
`FINAL_RESULTS.json`, using the replay CLI's `--report-policy` option.
Reproducing selection over the complete mixed corpus additionally requires the
authorized private cases; the public expansion alone is not the complete fit.

The reusable workflow is the installed scan/capture, strict import, automatic
selection, and explicit supplied-YAML write-back described by the portable
skill. Research scripts and historical question/error-policy experiments are
not alternative production workflows. Rule redesign and splitting remain
outside this change.

## Final verification

- Full suite: 396 tests passed after merge-review corrections.
- Ruff format/check, type checking, and `git diff --check` passed.
- Wheel and source archive built successfully.
- A fresh environment installed the wheel, parsed all 108 expanded sources
  offline with zero failures, and replayed all 108 public cases as comparable.
- The source archive contains the public source fixtures and complete portable
  skill; its eight corpus tests passed from the extracted archive.
- The separate-project CLI integration test exercises final capture, label
  import, automatic selection, and actual YAML write-back using mocked transport.
- The selector's final authority/grouping review found no remaining blocker.
- The exact frozen policy hashes match the installed packaged defaults.
- Strict returned-model/prompt compatibility reproduces the original frozen
  selection exactly; the predeclared future recall objective is separate.
- Calibration outputs reject input, metadata-sidecar, symlink, and hard-link
  collisions before writing. Captured not-applicable dispositions must agree
  with production assessment unless canonical model routing is recorded.
- Rust callable attribute lookup now walks adjacent preceding siblings instead
  of rescanning all siblings. At 4,000 ordinary functions, the isolated helper
  comparison changed from 2.527 s to 0.0041 s; full parsing took 0.100 s.

Live semantic measurements and mocked workflow tests are separate evidence.
Nothing was published or pushed as part of this verification.
