# Focused-question experiment plan

> This predeclared plan is retained as an experiment record. The completed
> development results are in
> [FOCUSED_QUESTIONS_RESULTS.md](FOCUSED_QUESTIONS_RESULTS.md). No candidate
> passed, the held-out split was not executed, and the one-off runner was
> removed rather than retained as a second calibration framework.

This is the predeclared plan for the repository-only focused-question
experiment. It records the procedure and candidate definitions before any new
provider measurement. It is not a result report, and the fixture annotations
are provisional synthetic review labels rather than human ground truth.
Round 2 added isolated JEV04 pair-bound candidates; round 3 predeclares one
final wording-only JEV04 pair candidate. Neither round replaces or modifies
the packaged JEV04 rule.

## Fixed inputs and guardrails

- Starting commit: `b13bb159c62d688b6248773b2e858fa3c02cf98`.
- Requested model: `jev-1.13.0`, pinned for every candidate and phase.
- Input price: USD `0.042` per million input tokens; output tokens are free for
  this budget.
- Cumulative experiment cap: USD `0.10`, including every attempt, retry,
  unsuccessful request, and localization request. Reserved input cost is
  calculated first, then a `25%` localization overhead is reserved:
  `reserved_tokens * 0.042 / 1_000_000 * 1.25 <= 0.10`.
- Development search: at most three rounds. A round may revise candidate
  wording only against development evidence. The selected candidate and all
  question/report hashes are frozen before any held-out execution.
- No labels, expected answers, or adjudication text enter provider state.
  Source parsing, context selection, question binding, request batching, and
  answer replay use jevscan's production contracts.
- The utility records `reserved_input_tokens`,
  `actual_provider_input_tokens`, reported cost, and reserved cost separately.
  A plan with no provider capture has `actual_provider_input_tokens: null`; it
  must not be described as measured usage.

The provider documentation describes independent questions sharing one state
and mixed Choice/Score/Noul requests:
[Primitives](https://docs.typesafe.ai/primitives.md),
[advanced EntryType structure](https://docs.typesafe.ai/primitives/advanced.md),
[structured state](https://docs.typesafe.ai/concepts/state.md), and the
[fan-out pattern](https://docs.typesafe.ai/patterns/fan-out.md). This
experiment uses jevscan's narrower string question schema and ordinary custom
rules rather than adopting a permanent grouped-question schema.

## Candidate definitions

Every candidate below is emitted as one or more ordinary `project` rules by
`calibration/focused_experiment.py`. Each child is a separate question in the
same production request when evidence is identical. The baseline child is
copied from the packaged rule at the starting commit, including its
applicability and report policy.

### JEV04 redundant validation

| Candidate | Ordinary rule IDs | Definition |
| --- | --- | --- |
| `jev04-baseline` | `FOCUS_JEV04_BASELINE` | Packaged JEV04 Choice question and report policy unchanged. It asks for `demonstrably_redundant`, `justified_or_absent`, or `insufficient_context`. |
| `jev04-focused-joint` | `FOCUS_JEV04_JOINT` | The same Choice contract, with this task text: “Select exactly one outcome for repeated validation in the target. Choose demonstrably_redundant only when the same invariant is visibly established and checked again without a new boundary, mutation, or concurrency risk. An await, callback, external call, mutation, or possible aliasing boundary can invalidate an invariant; immutable captured values and sequential cohesive lifecycle phases do not create a boundary. Choose insufficient_context only when the repeated check is visible but the missing guarantee determines the result.” Criteria and report gates remain the packaged JEV04 criteria and gates. |
| `jev04-focused-decomposed` | `FOCUS_JEV04_DECOMPOSED_GUARANTEE`, `FOCUS_JEV04_DECOMPOSED_PRESERVATION` | Two ordinary Noul children, both retaining JEV04 applicability. The guarantee child asks whether the same validation invariant is visibly established before the later validation. The preservation child asks whether the invariant remains preserved between checks without await, callback, external call, mutation, aliasing, or concurrency risk. The analysis composition is conservative AND: both child review signals are required; probabilities are never multiplied. |
| `jev04-pair-joint` | `FOCUS_JEV04_PAIR_JOINT` | Round-2 ordinary Choice rule using the focused joint meanings and the packaged JEV04 report policy. The task is dynamically bound to exactly one bounded `ValidationPair` extracted from the original unit target. It explicitly asks the model to judge only that pair and the same value/state relation. |
| `jev04-pair-preservation` | `FOCUS_JEV04_PAIR_PRESERVATION` | Round-3 final ordinary Choice rule using the packaged JEV04 Choice/report contract and the exact same `ValidationPair` binding. It asks whether both checks concern the same invariant and value/state, and whether the supplied code can invalidate that relationship. A callback, await, or call matters only when it can mutate, alias, or replace the checked value/state; a captured primitive immutable value that is never reassigned remains preserved across a callback. No filename- or language-specific exemption is introduced. |
| `jev04-pair-decomposed` | `FOCUS_JEV04_PAIR_DECOMPOSED_GUARANTEE`, `FOCUS_JEV04_PAIR_DECOMPOSED_PRESERVATION` | Round-2 ordinary Noul children using the corrected guarantee and preservation criteria above. Both children receive the exact same pair binding and are composed only as a diagnostic conservative AND; probabilities are never combined. |

Pair metadata is question/provenance metadata only. It contains the candidate
and pair IDs, source path, exact earlier/later operation line and byte spans,
the exact intervening line and byte span, and callable-boundary annotations.
It points into the complete unchanged production evidence and does not copy
predicate or intervening source bytes, invent a rationale, or create a new
target.

### Round-2 eligibility, fallback, and composition

The `plan` and `capture` commands accept repeatable `--candidate` filters.
Development may select only the pair candidates for a pair-focused purchase. A
held-out invocation still requires the development freeze and rejects any
candidate that is not frozen. Selected candidates are recorded in plans and
capture ledgers, so a pair-only capture cannot silently repurchase baseline
questions.

Pair questions are purchased only when extraction returns exactly one bounded
pair and every pair location is covered by the unchanged requested evidence.
Zero pairs, multiple pairs, occurrence/group/pair caps, parse recovery,
unsupported grammar or validation shape, ambiguous ownership, invalid target,
and incomplete evidence are explicit `whole_target_fallback` outcomes with a
stable reason. They are not omitted and are never interpreted as a clean
pair result. The extractor outcome record is written alongside capture output.

### Round-3 predeclaration and acceptance target

Round 3 is the final development wording round and is based on the current
development candidate at commit `a16c112`. It may change only the question
wording/question identity of `jev04-pair-preservation`; evidence, target
identity, exact pair binding, extractor, report policy, and location metadata
must remain unchanged. It must not alter packaged production rules or add
language- or filename-specific exemptions.

The complete round-3 task is concise rather than an appended addendum so the
full pair-plus-fallback production decision remains within the predeclared
input-overhead ceiling. This wording-only reduction does not change the
criteria, answer labels, evidence, or acceptance target.

The expected development success is: `summit.js` must become a production
replay signal for the eligible positive, and all five eligible negatives must
remain clean. This is an acceptance target declared before any inference, not a
result. No evidence or report artifacts change, and no provider inference is
part of this implementation round.

For candidate-level evaluation, replay combines pair-bound records for eligible
parents with already captured `jev04-baseline` records for ineligible parents.
The combined result reports eligible and fallback counts and is incomplete when
the required baseline fallback records are unavailable. The focused joint or
preservation question is used only for eligible pair records; whole-target
fallback keeps the deployed JEV04 Choice/report behavior. Pair-decomposed composition remains
diagnostic and requires matching pair IDs and matching question metadata in
both children before applying conservative AND.

### JEV01 mixed responsibilities

| Candidate | Ordinary rule ID | Definition |
| --- | --- | --- |
| `jev01-baseline` | `FOCUS_JEV01_BASELINE` | Packaged JEV01 Noul question and report policy unchanged. |
| `jev01-focused` | `FOCUS_JEV01_FOCUSED` | “Does the target itself interleave two or more independently meaningful policies or state transitions in a way that materially harms understanding? Count truly interleaved responsibilities. Do not count a cohesive lifecycle coordinator, sequential delegation, cleanup, callback or await boundaries that belong to one operation, or immutable captured values by themselves.” The packaged Noul report policy remains unchanged. |

### JEV02 unclear control flow

| Candidate | Ordinary rule IDs | Definition |
| --- | --- | --- |
| `jev02-baseline` | `FOCUS_JEV02_SCORE` | Packaged JEV02 Score question and report policy unchanged. The score remains an extent signal; expected scores are not reinterpreted as a binary category. |
| `jev02-presence-gated` | `FOCUS_JEV02_SCORE`, `FOCUS_JEV02_PRESENCE` | Retains the existing Score child as the extent signal and adds a separate Noul gate: “Is there a concrete control-flow structure in the target that obscures an important execution transition? Answer true only for an actual traceability problem. A merely local guard, one uncomplicated nesting level, a cohesive lifecycle, or an immutable captured value is false; the separate score question measures extent.” The gate has explicit true/false criteria matching that task and intentionally reuses the packaged JEV01 Noul report thresholds (warning at minimum probability `0.5`, error at `0.9`) because it is a binary JEV01-shaped review signal. A candidate review signal requires the explicit presence gate and the existing Score assessment; no score-to-category conversion is used. |

## Corpus and split

The public fixture corpus is
[`examples/calibration/focused`](../examples/calibration/focused/README.md).
Only `examples/calibration/focused/sources/` is scanned. Its manifest and all
labels are outside that root. There are six fresh independent groups for each
affected rule. JEV01 and JEV04 have three provisional positive and three
provisional negative groups; public JEV02 review has two positive and four
negative groups. Related variants must stay in one group; no source filename or
source text contains a label.

Development uses references to reviewed public expanded cases rather than
copying their source where possible. The references include `flint.ts`,
`raven.rs`, and the known `quarry.ts` and `summit.js` JEV04 cases. Although
`quarry.ts` and `summit.js` were marked held out in the older expanded
manifest, this experiment treats both as development diagnostics only. They
are never fresh held-out support and do not count toward the six fresh groups.

The source snapshot is SHA-256 over sorted relative filenames, each followed
by a NUL byte and its raw UTF-8 bytes. The focused manifest records the
resulting digest. The split identity is the SHA-256 of the raw manifest bytes;
the freeze record must match that digest before held-out planning. Split
isolation is by manifest group, not by source file or case row: one group
cannot occur in both phases.

The fixture deliberately includes callback/await boundaries, immutable
captured values, cohesive lifecycle orchestration, truly interleaved
responsibilities, obscure versus merely local control flow, and redundant
versus boundary-justified validation. These are controlled synthetic
diagnostics, not a population sample.

The focused JEV02 held-out annotations retain the reviewed expected levels:
Hazel `3`, Juniper `1`, Maple `2`, Oak `0`, Pine `0`, and Spruce `0`. Public
source review therefore supplies two positive groups (Hazel and Maple) and four
negative groups; private held-out additions must supply any further
independently reviewed positive support. JEV02 positive/negative coverage is
defined by expected level `>= 2`, not by a generic label; the presence child is
expected true only for those same levels. Juniper's explicit local branches
were reviewed as level 1 and are not warning-worthy.

## Metrics

Metrics are computed from captured calibration cases after production
`replay_case`; the experiment does not implement a second assessment engine.
They are reported by candidate, rule child, and split:

1. **Case recall:** positive (`Agree`) cases with a confirmed or tentative
   review signal divided by positive cases.
2. **Group hit coverage:** positive groups with at least one review signal
   divided by positive groups.
3. **Averaged within-group recall:** first compute positive-case recall inside
   each positive group, then average those group recalls so a group with many
   variants cannot dominate.
4. **False-review workload:** count and rate of review signals on `Disagree`
   cases. `Partial` is not silently counted as either class.
5. **Partial:** count and fraction of `Partial` cases, retained as an
   uncertainty result.
6. **Severity:** confirmed finding counts by `warning` and `error`; tentative
   severity is retained when present in the replay record. Severity agreement
   against an adjudicated severity is reported only where that field exists.
7. **Target attribution:** among review signals, the fraction whose production
   finding target exactly matches the captured target identity (scope, path,
   qualified name, and span). A missing finding is not a false attribution.
8. **Actual provider input usage:** reported input tokens from provider
   receipts, compared with reserved input tokens and cost. Missing receipts
   remain missing; estimated or reserved usage is not reported as actual.

For decomposed JEV04 and presence-gated JEV02, child metrics are always
reported separately. The conservative AND/gate summary is an analysis of
aligned child records only and does not change jevscan assessment behavior.

## Selection and acceptance rules

1. Validate source hash, parser success, target identity, Planner eligibility,
   evidence identity, question identity, model identity, and split isolation
   before capture. Applicability skips are not negative cases.
2. Compare candidates on identical source target and evidence state. Do not
   select from held-out answers, and reject held-out planning/execution unless
   a development freeze record matches the manifest hash.
3. A candidate is eligible for promotion only if it has at least three fresh
   positive and three fresh negative held-out groups for its affected rule,
   uses only the pinned model, stays below the cumulative cap including
   overhead, and has exact target attribution for every reported signal.
4. Prefer a candidate only when development evidence shows lower false-review
   workload without reducing positive case recall or group hit coverage versus
   its baseline. A genuine precision/recall tradeoff is reported and is not
   accepted as an unannounced improvement.
5. On held-out data, accept only a pre-frozen candidate that preserves the
   declared recall and group-coverage floors, has zero permitted increase in
   false-review workload versus its baseline, and does not turn `Partial`
   cases into asserted positives. Any missed positive, negative signal,
   severity disagreement, target-attribution mismatch, split leak, model
   mismatch, or usage discrepancy is reported explicitly.
6. No candidate result changes packaged defaults or production runtime in this
   experiment. Promotion, if any, requires a separate product decision with
   measured evidence.
