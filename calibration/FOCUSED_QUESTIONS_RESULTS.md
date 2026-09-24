# Focused-question experiment results

## Decision

No candidate earned promotion. The packaged rules and the maintainer-approved
thresholds from PR #12 remain unchanged.

The three permitted development rounds were completed with `jev-1.13.0`.
JEV01 and JEV02 decomposition did not improve their baselines. Binding JEV04 to
one exact repeated-validation pair removed one false review but did not recover
the diagnostic `summit.js` positive. A final preservation-focused wording
recovered every development positive but produced two false reviews, including
the known `quarry.ts` negative. This violated the predeclared requirement that
false-review workload must not increase.

Because no development candidate passed, no candidate was frozen and no
held-out answer was purchased or inspected. The held-out support counts and
labels therefore do not appear as measured results.

## Fixed experiment identity

| Field | Value |
| --- | --- |
| Starting commit | `b13bb159c62d688b6248773b2e858fa3c02cf98a` |
| Requested model | `jev-1.13.0` |
| Returned model | `jev-1.13.0` for every response |
| Private split-manifest SHA-256 | `omitted digest` |
| Development rounds | 3 of 3 |
| Provider price used | USD 0.042 per million input tokens; output free |
| Cumulative cap | USD 0.10 |

All development source was public and pinned to its recorded commit. Labels,
adjudication reasons, and expected scores were outside the scanned source root
and were not sent to the provider. The private manifest also described the
unopened held-out split; it remains uncommitted because it contains local source
locations.

## Development results

Review-list recall counts both confirmed and tentative production replay
signals. False-review workload is the number of `Disagree` cases that produced
a review signal. Group coverage is reported separately from case recall.

### Round 1: whole-target questions

| Rule and candidate | Cases | Case recall | Group coverage | False reviews | Decision |
| --- | ---: | ---: | ---: | ---: | --- |
| JEV01 packaged baseline | 12 | 0.000 | 0.000 | 0 | Baseline retained |
| JEV01 focused joint | 12 | 0.000 | 0.000 | 0 | Reject: no positive recovery |
| JEV02 packaged Score | 12 | 0.833 | 0.833 | 0 | Baseline retained |
| JEV02 Score plus presence gate | 12 | 0.000 | 0.000 | 0 | Reject: gate removed supported positives |
| JEV04 packaged baseline | 12 | 0.750 | 0.667 | 1 | Baseline |
| JEV04 focused joint | 12 | 0.500 | 0.667 | 0 | Reject: lower case recall |

The unbound JEV04 decomposition remained diagnostic-only. Its equivalence child
produced seven false reviews; its preservation child retained 0.750 case recall
with no false reviews. The children could not establish one compound defect
without a shared candidate relationship, so their probabilities were not
combined.

### Round 2: exact pair binding

The extractor found exactly one bounded, byte-identical repeated-predicate pair
for 8 of 12 JEV04 development cases. The other four used the unchanged packaged
whole-target baseline as the declared production fallback.

| Candidate | Eligible pair cases | Combined case recall | Combined group coverage | False reviews | Decision |
| --- | ---: | ---: | ---: | ---: | --- |
| Pair-bound focused Choice | 8 | 0.750 | 0.667 | 0 | Reject: below 0.80 floors |
| Pair-bound decomposed diagnostic | 8 | 0.750 | 0.667 | 0 | Reject: diagnostic only and below floors |

Pair binding fixed the false review on `quarry.ts`, but still missed
`summit.js`. The `summit.js` answer selected `justified_or_absent` with low
confidence even though the repeated predicate reads an immutable captured
boolean that is never reassigned. Exact target attribution was 1.0 for every
emitted signal.

### Round 3: preservation-focused pair wording

The final wording clarified that a callback, await, or call matters only when
it can mutate, alias, or replace the checked value/state. It used the same
source, evidence, target, pair, criteria, report policy, and location metadata
as round 2.

| Candidate | Eligible pair cases | Combined case recall | Combined group coverage | False reviews | Decision |
| --- | ---: | ---: | ---: | ---: | --- |
| Pair-bound preservation Choice | 8 | 1.000 | 1.000 | 2 | Reject: false workload increased |

This recovered `summit.js`, but `quarry.ts` and `plume.ts` became confirmed
false reviews. The candidate therefore failed both its round-specific condition
that all five eligible negatives remain clean and the general requirement of no
increase over the baseline's one false review.

There were no `Partial` development cases. No severity mismatch or target
attribution error was observed, but these facts do not compensate for the
failed detection tradeoff.

## Usage and cost

| Round | Requests | Purchased questions | Provider input | Provider output | Reported input cost | Conservative reservation |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 36 | 96 | 44,983 | 3,051 | USD 0.001889286 | USD 0.007005180 |
| 2 | 8 | 24 | 17,644 | 841 | USD 0.000741048 | USD 0.002457588 |
| 3 | 8 | 8 | 9,286 | 511 | USD 0.000390012 | USD 0.001351098 |
| **Total** | **52** | **128** | **71,913** | **4,403** | **USD 0.003020346** | **USD 0.010813866** |

Reservations include one retry and the 25% margin. They are not invoices.
Provider-reported input usage is the measured billable basis; output was free
under the verified rate.

The complete planned JEV04 production decision, including four packaged
whole-target fallbacks, was:

| Design | Planned input tokens | Change from baseline |
| --- | ---: | ---: |
| Packaged whole-target baseline | 14,774 | — |
| Round-2 pair Choice plus fallback | 17,670 | +19.60% |
| Round-3 pair Choice plus fallback | 17,734 | +20.04% |

Pair-only live costs exclude fallback calls because the existing baseline
answers were reused. The full planned totals above use the original 12
target-rule opportunities and do not improve the ratio by counting purchased
subquestions as additional coverage.

## Evidence-localization result

For every eligible pair, parser-owned metadata identified:

- the earlier validation operation;
- the exact intervening byte and line span;
- the later validation operation; and
- whether the relation crossed a callable boundary.

All emitted spans resolved to the frozen source, including Unicode and nested
callback tests. No model-generated line number or explanation was used.
Unsupported syntax, parse recovery, ambiguous ownership, multiple pairs, caps,
or incomplete evidence produced an explicit whole-target fallback rather than
a clean verdict.

Localization was exact for the measured candidates, but it was not promoted as
output-only functionality because no accepted detector used it. A precise
location does not make the model's interpretation of that location correct.

## Limits

- The development set is small and deliberately diagnostic, not a population
  sample.
- Known failures, including `quarry.ts` and `summit.js`, are development cases.
- No held-out result exists because development acceptance failed.
- Real-project labels were collected and frozen, but their answers were not
  purchased or inspected.
- The experiment establishes that these candidates did not earn promotion. It
  does not establish that relationship-bound questions cannot improve JEV04.
- No claim is made that independently evaluated questions have statistically
  independent errors.

The machine-readable aggregate is
[`FOCUSED_QUESTIONS_RESULTS.json`](FOCUSED_QUESTIONS_RESULTS.json). The
predeclared contracts and candidate definitions are in
[`FOCUSED_QUESTIONS_PLAN.md`](FOCUSED_QUESTIONS_PLAN.md).
