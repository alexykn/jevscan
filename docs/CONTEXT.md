# Source preparation and bounded context recovery — RC5

## Goals and invariants

Jevscan should not discard a file merely because its byte-based token estimate is inaccurate. It also must not pretend that source fits the provider when it does not, trim arbitrary bytes, or describe a partial target as complete. The path is therefore: attempt requested evidence, recover explicit size failures, prepare exact AST fragments around a complete unit, and report coverage honestly.

The complete target is mandatory. Everything else is surrounding evidence with recorded provenance. A file target includes every byte of that file. File-level duplication/ownership judgments are not replaced with an average of sampled units or a model-generated summary. If an intact file cannot be accepted, it is explicitly unevaluated; fitting nested units remain eligible.

Configuration remains additive YAML v4. Numerical finding thresholds, applicability rules, tentative severity semantics and uncertainty-router gates are unchanged. Report schema is 6 because preparation and coverage events are new.

## Provider facts and research

Primary materials retrieved on **2026-09-18**:

- [TypeSafe Models](https://docs.typesafe.ai/models.md): Jev 1.13 permits 32k tokens for state plus the longest question and 64k for the combined state/questions. The model-list API does not expose an exact local token counter. Larger byte heuristics are not permission to exceed these capacities.
- [API](https://docs.typesafe.ai/api.md), [SDK exceptions](https://docs.typesafe.ai/sdk/python/api/exceptions.md), and [OpenAPI](https://api.typesafe.ai/openapi.json): distinguish validation, authentication, rate/overload and server errors. No canonical oversized-context JSON payload was documented in these fetched sources. The handler's explicit code envelopes are compatibility support, not fabricated observations from a live request.
- [Jev 1.13 jaggedness](https://docs.typesafe.ai/model-jaggedness/jev-1.13.md): irrelevant state and extra indirection can reduce accuracy. The provider recommends filtering relevant input and keeping arithmetic/structural identities in code. Passing a large request is not proof that it is the best evidence.
- [State](https://docs.typesafe.ai/concepts/state.md), [Noul](https://docs.typesafe.ai/primitives/noul.md), [fan-out](https://docs.typesafe.ai/patterns/fan-out.md), and [reranking](https://docs.typesafe.ai/cookbooks/rerank_typesafe.md): structured named evidence, independent questions, and relevance judgments composed by the application. Our source-selection query includes the actual active rule, target and candidate, rather than relying on a question key to convey semantics.
- [cAST, Findings of EMNLP 2025](https://aclanthology.org/2025.findings-emnlp.430/): AST boundaries can preserve code structure better than line-only chunking. Its evaluations concern retrieval/code generation, not Jev lint accuracy; it motivates structural candidates, not a correctness claim for this implementation.
- [RepoCoder](https://arxiv.org/abs/2303.12570): retrieval of repository context can support code tasks. We do not adopt an unrestricted iterative agent loop; retrieval and preparation remain bounded and separately accountable.

These documents were captured through a read-only GitHub Actions reference workflow because direct web fetching returned cache misses. No authenticated Jev evaluation was performed. The published limits correct the earlier assumption that a ~46k estimate should simply be presumed acceptable. RC5 permits a bounded attempt because the estimate can be wrong, not because the actual service limit disappeared.

## Limits and recovery

`max_request_bytes` (default 1 MiB) and `max_questions` (64) remain hard limits. The optional local `max_context_tokens` and `max_total_tokens` estimates default to null; copied project values remain effective until changed. Estimates still count JSON escaping, metadata, instructions and criteria. State plus a long question matters, not source length alone.

Recognized HTTP 413 and exact structured `max_tokens_exceeded` codes at 400/422 enter recovery. Other validation errors do not. The request loop first bisects independent question batches without modifying source. At a singleton rejection, a unit check may prepare smaller evidence; a file check or `oversized_context: skip` cannot. The executor retains successful answers and never counts an answer twice.

Once one singleton rejects a particular source state, other unit checks with exactly that state can skip repeating a large initial probe. This is a conservative **file-local preparation policy**, labelled `same evidence rejected for another check`, not a claim those individual questions were submitted or rejected. Nothing is persisted as a negative provider cache. File-level checks still get their own complete-target attempts when local bounds permit.

Preparation starts with at most `recovery_context_tokens` (28000 by default), the explicit local ceiling if smaller, and 60% of the previous rejected singleton estimate if available. Its byte ceiling is strictly below the previous body size. At most three structural preparation rounds are allowed; a final complete-target-only request may be attempted if smaller and permitted. A rejected target-only request ends as an omission. Question splits preserve the round number. Neither hidden clipping nor identical primary-body retry can create an endless loop.

Only sanitized status/code/request-ID and a body hash enter the rejection audit. Arbitrary response prose and echoed source/credentials are excluded. Unknown error shapes fail visibly rather than guessing at a recovery. 429/529/network retries follow the existing independent backoff policy and are not treated as size signals.

## Structural evidence construction

`syntax.py` records immutable AST ranges for declarations, imports, fields and lexical setup operations during the existing parser pass. Native parser nodes never escape the parser worker. The old bounded import-string fallback has been removed. `context.py` assembles original UTF-8 slices as separate source documents; it does not rewrite code or insert fake method bodies.

`compaction.py` starts from the complete unit. It collects lexical owner headers and visible imports, fields/declarations referenced by the unit, referenced same-file functions/types, related owner members (including constructor setup), and surrounding lexical setup. At most two reference levels are followed. Names/scopes are hints, not type resolution or a call graph. Nested callbacks keep their enclosing source-derived identity and captured declarations when selected.

The candidate pool is capped (32 by default). Owner/import fragments are selected structurally first. Direct references precede transitive/setup and sibling candidates. When all candidates fit, no relevance call is needed. If residual candidates do not fit, the active YAML rule's full primitive, instructions and criteria are bound into independent Noul selection questions. This works for a custom rule with arbitrary name/criteria; production code does not know JEV04, JEV05 or any other built-in ID.

Model relevance ranks candidates within structural priority groups; it does not overrule the mandatory target or certify omitted source as irrelevant. Unranked candidates remain candidates, with a neutral ordering value rather than an invented provider answer. Low relevance is not a safety proof. Selection considers evidence for either possible verdict, not evidence that supports an earlier answer. There is no earlier answer in initial preparation state.

Candidate previews are bounded and explicitly partial. Admitted code is the complete selected AST span; owner headers are labelled as signature fragments. Exact overlapping spans are unioned. A bounded source outline can include up to 16 omitted-unit signature previews when it fits, explicitly not replacement implementations. Outline entries and omissions are counted in code. Later enrichment drops the old outline rather than preserving stale statements about missing implementations.

Source identity uses the entire encoded evidence state. Equal outer bounds do not imply equal evidence when different internal spans are omitted. Coverage checks the union of actual original-file ranges, not a reconstructed document's length or a model's assurance. Compacted surrounding evidence is incomplete whenever the original requested context is not all present, even if selection plausibly retained the useful parts.

## Boundaries, resources and provenance

Preparation is same-file source selection, with no new filesystem traversal. Post-answer enrichment retains its existing allowed-root discovery and symlink restrictions. Both share the same relevance builder, HTTP client, validation, successful-answer cache and pacing. Compaction calls have a separate per-file cap of 12, including cache hits; setting it to zero retains deterministic AST preparation.

Auxiliary size failures split candidate batches, and unrankable singleton candidates remain unranked. Exhausting the optional selection budget falls back to the recorded structural order. Authentication, malformed responses, unrelated provider failures and cancellation are not swallowed as successful preparation. A failed run still emits completed results and marks unanswered checks.

The file executor queues small preparation records, not copies of every rejected full request. It prepares at most 64 items together and repacks equal evidence under the common request budget. Original source, parser metadata and a small envelope cache are file-local; the optional repository index has its own limits. Nesting, source size, candidate count and worker concurrency still affect memory. No unbounded context-recursion or whole-repository result collection is introduced.

Preparation audits include cause, source hash, request/rejection metadata, selection predictions/cache/model, selected source relationships, caps and omitted candidate spans. The final semantic question is unchanged. Relevance answers are not presented as code-quality findings and are not fed back as the desired verdict. Confirmed/tentative reporting and exit semantics remain those of the actual assessment plus coverage.

## Human output

Each affected file emits one `coverage` summary with reduced targets/checks and omitted targets/checks. Per-target evidence and diagnostic records remain complete in JSON/JSONL; they no longer interrupt text findings hundreds of times. Operational syntax/read/service errors stay visible, and default finding filtering still includes cyan tentative warnings/errors. File summaries are not subject to the finding display cap.

A `display_name` derives useful callback roles from syntax, such as `describe["renderer"].test["mounts"]`, `Promise[executor]`, or `queueMicrotask[arg0]@142:6`. Generic method callbacks use argument positions rather than claiming runtime dispatch semantics. Labels are bounded and terminal-safe. Canonical path/span/kind IDs, actual symbol names and qualified names are unchanged, so a test title never becomes a guessed callable for retrieval. Unrecognized roles keep a location label, and all previously scanned callbacks remain targets.

## What still needs live evaluation

Software tests can establish finite recovery, complete targets, exact bytes, valid question bindings, complete reports and working source identities. They cannot establish that the model's source ranking improves correctness. Known hard cases remain: dynamic dispatch, aliased names, required cross-file contracts, giant complete targets, unknown provider error encodings, and long states with distracting code.

Acceptance should compare pinned-model JSONL on representative code before/after preparation, including correct and defective examples and unrelated same-name candidates. Inspect the actual retained source, not only the number of warnings or enrichment calls. A successful large request is evidence of acceptance, not proof that all context was used accurately. A lower unknown count alone is not success.
