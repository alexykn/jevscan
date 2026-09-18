# Evidence enrichment in 0.2.0rc2

## Design and research basis

This workflow adds evidence, not repeated attempts to obtain a more confident answer. It is ordinary sequential Python: primary evaluation, conditional route, candidate relevance, then one fresh evaluation. There is no agent framework, arbitrary tool execution, persistent provider conversation, or recursive search.

Primary sources reviewed on 18 September 2026:

- [TypeSafe documentation introduction](https://docs.typesafe.ai/): shared state, independent typed questions, and application-controlled composition.
- [TypeSafe's current agent skill](https://github.com/typesafe-ai/skills/blob/65a39f393687675ce170e6094757de20370365b9/skills/typesafe-ai/SKILL.md): named structured state; explicit question meaning rather than relying on IDs; relevance selection; a second request when an earlier result is needed to fetch evidence; probabilities/concentration are not correctness guarantees.
- [Introducing System One and Jev](https://typesafe.ai/blog/introducing-system-one-models-and-jev): typed decisions instead of generated explanations, and composing focused judgments in code.
- [Official Python SDK question types](https://github.com/typesafe-ai/typesafe-sdk-python/blob/v0.6.0/src/typesafe_sdk/_core/question_types.py) and [endpoint request construction](https://github.com/typesafe-ai/typesafe-sdk-python/blob/v0.6.0/src/typesafe_sdk/_core/endpoints.py): named questions against text/JSON state, structured instructions and criteria, and typed answers.

The live documentation introduction and official skill were accessible; deeper state/confidence/reranking/function-calling cookbook URLs returned cache misses in the research environment. This document does not claim experimental results from those inaccessible examples. The architecture below is our implementation choice based on the accessible primary guidance, not a provider-prescribed code-review algorithm.

## Why this shape

A Noul near 0.5 is indecision about a proposition, not medium severity. Low Choice/Score confidence can reflect adjacent acceptable alternatives, a broad rubric, or insufficient evidence. These cases should not all trigger a caller search. The model's route is also a prediction: it cannot diagnose its own hidden internal cause with certainty.

The first stage asks which additional evidence would be useful, based on the actual source, target, and original question. `sufficient` means no missing source was identified and leaves the initial uncertainty unchanged. `unavailable` means source retrieval is unlikely to establish the missing runtime/external fact. A confidently selected `not_applicable` ends separately from OK. Weak routing predictions stop rather than guessing a path.

For missing source, the routes are possible callers, referenced definitions, test references, or enclosing owner/file. The caller supplies no prior verdict to the router. Selection likewise asks whether each candidate can help decide the rule, not whether it supports an expected conclusion. Candidate relevance uses independent Nouls instead of a single forced-choice winner: several candidates may be useful, and no candidate may qualify. Only questions sharing that candidate-batch state are batched; dependent stages remain separate calls.

On reassessment, the original target and question remain unchanged. The model sees original evidence plus admitted complete source, provenance, and coverage. It does not see the initial answer, router verdict, or relevance scores. This avoids explicitly anchoring the new judgment on the old one, but it does not prove the model will reason correctly. Stop after one reassessment, including an unchanged unknown result.

## Ownership

- `assessment.py` owns severity, applicability, and uncertainty reasons. Both initial and final answers use it.
- `inference.py` owns cached/validated prediction and usage accounting for every stage; `client.py` still owns HTTP, retry, and pacing.
- `retrieval.py` owns the lazy index, safe source reads, lexical relationship candidates, and bounded snapshot lifetime.
- `enrichment.py` owns the conditional route/selection/reassessment sequence. It uses `RequestBudget`, not a second token-size implementation.
- `evaluation.py` owns per-file results and the audit. It runs enrichment only for uncertain eligible checks and emits one final result per rule/target.

The shared index is built only on the first caller/definition/test request. It is read-only after loading and is retained only for this invocation. Enclosing-source retrieval uses the current file without a project scan. Existing fixed file workers share one index; there is no task per candidate. Files still finish independently and their target output remains grouped.

## Source evidence contract

Candidates must originate in local discovery under the resolved project root and pass the existing source-language/include/exclude/Git-ignore filters. Model outputs never become filesystem paths. Reads open each path component relative to the root without following symlinks, require a regular file, cap bytes, and reject detectable changes during a read. A selected candidate uses the same immutable bytes as its preview, not a second filesystem read. A separately indexed version of the primary file is not mixed into an older primary evaluation snapshot.

Every candidate has a content-dependent ID, path, target name, UTF-8 byte span, line range, full-file SHA-256, and relationship label. Call references also retain the matched occurrence. Previews are bounded and explicitly marked partial; final admitted evidence is a complete extracted source unit or complete file. Overlapping spans are unioned to avoid repeating source. Selected source is subject to the same final context/aggregate/byte/question budgets; it never displaces the original target or evidence.

The index records discovery completeness, files/bytes read, failures/oversized inputs, matched candidates, and cap omissions. That coverage accompanies the final state and report. A file snapshot is not an atomic repository snapshot. A changed file on the next invocation changes its candidate identity; selection and reassessment cache keys change accordingly. Evidence/report policy changes are still separated: a changed severity boundary need not repeat unchanged inference.

**Lexical evidence is not a call graph.** A syntactic `obj.run()` is a possible use of a target named `run`, not proof that dispatch reaches that target. References in comments or strings are not parsed as call expressions. Aliases, re-exports, inheritance, decorators, macros and runtime dispatch can leave candidates undiscovered or wrongly matched. Tests are selected by source references and file naming, not by observed execution. Rust impl names provide lexical hints; there is no rustc/type resolution. Cross-language links and external dependencies are not resolved. Selecting a few callers cannot establish a universal upstream invariant.

## Bounds and stop behavior

The defaults in `default.yaml` are policy starting points, not calibration results. Per-file review and prediction caps apply to cache hits too, keeping a cached run's logic consistent with an uncached run. Source-index limits apply globally to the invocation. Ordinary HTTP retries remain finite and are counted separately from prediction budgets.

Unknown results survive `route_uncertain`, `sufficient`, `unavailable`, `no_relevant_evidence`, `check_budget`, `call_budget`, `request_budget`, `evidence_budget`, and `provider_context_limit`. A malformed answer, authentication error, cache failure, or unrelated server failure is not a no-match outcome: it propagates to the scan's operational-error boundary. Already obtained judgments and the in-progress review trace are emitted before failure is reported. The model is never repeatedly queried until a desired verdict appears.

A partial evidence search is recorded but is not automatically an operational scan failure: primary target coverage is unchanged. A remaining unknown is not OK. If requested primary context was reduced, that original coverage limitation remains unless complete enclosing evidence is genuinely restored. A model-selected not-applicable status is explicitly distinguished from a conclusive negative finding.

## Audit and release acceptance

JSON/JSONL schema 3 exposes per-rule `reviews` and `uncertainty_reasons`. The audit includes the initial answer/model/evidence, routing and selection predictions, request hashes, cache flags, candidate metadata/relevance, admitted source locations/hashes, retrieval coverage, and the final stop outcome. It does not contain copied candidate previews or arbitrary model-generated explanations. Final findings/counts are not duplicated with the initial judgment. Text remains warning/error-first; verbose output shows unknown reasons, not-applicable results and evidence-review details.

No live authenticated semantic test was run for this release. Mocked provider tests validate the production orchestration and contracts, not whether more evidence improves Jev's accuracy. A useful acceptance comparison is:

```bash
# These commands send source to TypeSafe. Review root-level sharing first.
uv run jevscan src --no-enrichment --format jsonl -o primary.jsonl
uv run jevscan src --format jsonl -o enriched.jsonl
```

Pin an account-supported model and keep source/configuration unchanged for the comparison. Include known defects, clean cases, same-name unrelated methods, and genuinely unavailable contracts. Inspect changes in false positives/negatives, uncertainty resolution, not-applicable routing, evidence relevance, requests and latency. A lower question-mark count alone is not success. Fewer wrong confident answers matters more than manufacturing certainty.
