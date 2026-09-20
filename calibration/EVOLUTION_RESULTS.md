# Semantic rule evolution results

No candidate met every predeclared promotion condition. After reviewing the
measured tradeoff, the product owner accepted the partial-transition wording-v2
result for production as JEV10: it retained 9/9 development positives, removed
both clean false reviews, and retained one tentative signal on an intentionally
unresolved case. JEV01–JEV09 and their reporting policies remain unchanged.

The work produced two reusable, non-production assets:

- `calibration/evolution-candidates.yaml` records the tested question contracts.
- `examples/calibration/evolution/` provides a public development corpus for
  unaccounted partial state transitions.

Private manifests, request material, answers, receipts, and the cumulative
ledger remain under ignored `.jevscan-calibration/evolution/private/`.

## Method

- Model: `jev-1.13.0`.
- Prompt: production prompt policy.
- Historical held-out cases were treated as development because they had
  already been inspected.
- Labels and split metadata were excluded from provider requests.
- Candidate children sharing evidence were batched, but their probabilities
  were never multiplied.
- JEV01 and JEV04 were evaluated only through their deterministic parent
  compositions; child findings were not promoted.
- The strict plan would create fresh held-out data only after a candidate passed
  development and was frozen. No candidate passed that gate; the later product
  decision to ship JEV10 therefore has no fresh held-out result.

The first live round used 22 JEV01 parents, 14 JEV03 cases, 19 JEV05 cases,
15 JEV08 cases, 22 partial-transition cases, and 21 JEV04 parents. These are
development fixtures, not a production-population sample. Synthetic
translations and close variants share support groups.

All 135 provider requests completed with model `jev-1.13.0`; there were no
retries, errors, model mismatches, or request-privacy failures.

## Existing rules

| Rule | Candidate | Development result | Decision |
| --- | --- | --- | --- |
| JEV01 | Strict AND of responsibility breadth and visible interleaving | 0/9 Agree groups; 0/11 Disagree signals | Reject. Both children assigned low true probabilities to almost every positive. |
| JEV02 | No semantic mutation | Reporting-only analysis found no Pareto-safe change | Retain packaged rule. |
| JEV03 | Concrete inline mechanics wording | 3/7 Agree groups; 0/7 Disagree signals | Reject for recall. |
| JEV04 | Exact-pair guarantee and preservation composition | 1/9 Agree cases; 1/10 Disagree signals. Baseline was 7/9 and 2/10. | Reject. False workload improved, but recall collapsed. |
| JEV05 | Target-local postcondition wording | 1/8 Agree cases; 0/8 Disagree signals; 0/3 Partial signals | Reject for recall; six misses selected the wrong semantic outcome. |
| JEV06 | No semantic mutation | Existing misses were answer-routing failures; no supported applicability change | Retain packaged rule. |
| JEV07 | Transfer-aware wording considered offline | The labeled development set contains an unresolved near-duplicate contradiction | Do not purchase or promote a mutation. |
| JEV08 | Three-way owner-cohesion Choice | 1/7 Agree cases; 0/8 Disagree signals | Reject for recall; most misses selected `cohesive_owner`. |
| JEV09 | No semantic mutation | Existing contract already distinguishes duplicated behavior from separate boundaries | Retain packaged rule. |

### Rejected reporting-only fits

Two post-hoc operating points separated the observed development values but
were not accepted:

- JEV01 could reach 8/9 Agree groups with strict-AND child gates of `0.26`
  and `0.11`. Those gates would report the parent while each child still
  predominantly answered false, and the corpus had no Partial controls.
- JEV03 could reach 6/7 Agree groups with a warning threshold of `0.18`,
  one hundredth above the largest negative value. That would report an
  18%-true Noul answer and conflict with the probability contract.

These are examples of development-set separation, not valid reporting
policies. They were not sent for another paid round.

## New-rule search

### Unaccounted partial state transition

The proposed Choice asks whether a target performs multiple semantically
coupled externally observable effects, can expose only a subset after failure,
and lacks atomic publication, rollback, compensation, reconciliation, or a
valid progressive contract.

The public corpus contains 22 cases in 15 support groups:

- 9 Agree cases in 3 groups;
- 9 Disagree controls;
- 4 Partial cases with omitted helper or resource contracts.

Round 1:

| Measure | Result |
| --- | ---: |
| Agree case recall | 9/9 |
| Agree group recall | 3/3 |
| Disagree signals | 2/9 |
| Partial signals | 1/4 tentative |

The false reviews were the explicit restartable-progress and independent-audit
controls. A second-round wording clarification named those existing exclusions
without changing the Choice schema, target, evidence, or report gates.

Round 2:

| Measure | Result |
| --- | ---: |
| Agree case recall | 9/9 |
| Agree group recall | 3/3 |
| Disagree signals | 0/9 |
| Partial signals | 1/4 tentative |
| Exact attribution | 10/10 |

The frozen gate required zero signal, tentative or confirmed, on every Partial
case. `evo-020` still produced a tentative signal, so the candidate failed that
strict gate. No further wording or threshold adjustment was made after seeing
that result. The product owner subsequently accepted this measured tradeoff:
JEV10 uses wording v2 and keeps the tested probability/confidence gates. A
static admission fact limits it to callables with multiple visible
effect-shaped operations; Jev still decides whether those operations are
externally observable, coupled, and failure-exposed. Fresh held-out evaluation
was not run before this production decision.

### Other proposed dimensions

| Proposal | Decision |
| --- | --- |
| Cancellation-safe transitions | Fold into partial-state-transition semantics; it is not an independent experiment. |
| Truthful aggregate outcomes | Potentially distinct, but the reviewed repositories did not provide three independent positive support groups. |
| Authority-boundary leak | Reject: either duplicates JEV07 authority ownership or becomes a generic access-control/security rule. |
| Deferred postcondition | Reject in its broad form: overlaps partial transitions and intentional asynchronous designs, with no independent positive support. |

## Cost and accounting

| Round | Requests | Input tokens | Output tokens | Reported cost | Reserved cost |
| --- | ---: | ---: | ---: | ---: | ---: |
| Six-candidate development round | 113 | 127,412 | 7,057 | $0.005351304 | $0.019986582 |
| Partial wording round 2 | 22 | 24,302 | 1,305 | $0.001020684 | $0.004053420 |
| **Total** | **135** | **151,714** | **8,362** | **$0.006371988** | **$0.024040002** |

The task-wide cap was $1.00. No selected-real or held-out provider calls were
made after the development failure.

## Validation and scope

- Candidate replay rejects wrong candidate provenance, unexpected or
  incomplete child bundles, mismatched target/evidence/prompt identities, and
  malformed exact-pair metadata.
- Whole-target JEV04 fallback findings retain their severity and attribution;
  candidate child findings remain suppressed.
- Exact-pair metadata is bound to the captured target path and span.
- Public corpus contracts validate source digests, target identities, support
  groups, comparison scopes, and executable Python/Perl fixture behavior.
- Focused and public-corpus tests: 55 passed.
- Ruff format and lint: passed.
- `ty` type checking: passed.
- `git diff --check`: passed.

These results measure initial production Planner/ContextBuilder judgments on a
small development population. They do not estimate general production accuracy.
