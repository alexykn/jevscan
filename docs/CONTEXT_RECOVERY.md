# Rule-aware context recovery — 0.2.0rc7

## Findings from the research

Sources reviewed on 2026-09-19:

- [TypeSafe's official agent guidance](https://github.com/typesafe-ai/skills/blob/main/skills/typesafe-ai/SKILL.md) distinguishes state from the question, requires complete meaning in instructions, recommends independent typed judgments for multiple candidates, and composes fresh requests after acquiring evidence.
- The [official Python SDK error handling](https://github.com/typesafe-ai/typesafe-sdk-python/blob/main/src/typesafe_sdk/_core/errors.py) preserves HTTP status/body and `x-typesafe-request-id`; its generated API models expose generic validation envelopes rather than one specialized context exception schema.
- The [Pydantic TypeSafe integration documentation](https://pydantic.dev/docs/ai/models/typesafe/) documents Jev 1.13 as **32k tokens for state plus the longest question** and **64k for state plus all questions**, and names `max_tokens_exceeded` for over-limit requests. It also warns that irrelevant context reduces accuracy.
- [Cloudflare's Jev model page](https://developers.cloudflare.com/ai/models/typesafe/jev/) independently lists a 32k context window for the hosted model. This is supporting evidence for the single-question window; jevscan uses the more specific 32k/64k split above for request planning.
- [Hierarchical Context Pruning (AAAI 2025)](https://ojs.aaai.org/index.php/AAAI/article/view/34782) motivates syntax/dependency-aware source selection rather than blind truncation. Its results concern repository code completion, **not** Jev code-review accuracy; no accuracy claim is transferred.

The live rc5 acceptance run supplied by the project is also an input to this design. It recorded thousands of omitted checks, `size_rejections=0`, `compaction_calls=0`, and then a generic HTTP 400. That established two application bugs: known model limits were no longer invoking preflight recovery, and an unclassified request-local 400 could cancel the concurrent scan. It did **not** establish the provider's exact 400 body because rc5 deliberately discarded unknown response bodies.

Direct TypeSafe documentation/schema pages were not reliably accessible through the research environment. No authenticated oversized Jev request was made for rc6, so the exact production error envelope remains an acceptance item. The implementation distinguishes published limit semantics, safe structured compatibility handling, and application recovery policy.

## Model-aware preflight

The packaged Jev-1.13 planning thresholds are 28,000 tokens for state plus the longest question and 56,000 aggregate, leaving headroom below the documented 32,000/64,000 provider ceilings. `RequestBudget` also carries the provider ceiling for `jev-latest`, `jev-preview`, `jev-1.13`, and `jev-1.13.0`. Thus setting a local threshold to `null` removes only the extra margin; it does not make a known Jev-1.13 request unbounded. Explicit local values above the known provider ceiling are capped at that ceiling. Unknown model IDs use the configured limits without inventing provider limits.

The estimator is intentionally approximate: encoded byte length divided by `bytes_per_token`, plus a reserve. Successful Jev responses expose actual `usage.input_tokens`. rc6 keeps one run-local `TokenCalibration` shared across file evaluators and uses observed successful requests only to **tighten** the byte/token estimate. The calibration cannot enlarge a request budget, is not persisted, and never replaces the provider ceiling.

Preflight distinguishes four independent local constraints: state+longest-question (`context`), state+all-questions (`total`), question count, and serialized request bytes. Recovery follows the information available:

- aggregate/question-count/byte packing overflow with multiple questions and a fitting context: split the question batch while keeping source intact;
- context overflow: compact unit evidence before HTTP;
- both context and aggregate overflow: compact source, then repack questions;
- complete file/class/impl target that cannot fit: explicit omission rather than scoring fragments;
- `oversized_context: skip`: explicit omission without compaction.

This restores the useful part of the pre-rc5 design: known limits prevent doomed requests. The rc5 AST/rule-aware compactor remains the recovery mechanism, replacing the old file→owner→target fallback.

## Provider rejection and request isolation

Provider rejection remains a second line of defence for estimator/tokenizer mismatch. HTTP 413 is a size signal. At HTTP 400/422 rc6 recursively inspects only bounded machine fields (`code`, `type`, `status`, and scalar machine-like `error` values) and treats the exact value `max_tokens_exceeded` as a size signal. Nested validation forms such as `detail[].type=max_tokens_exceeded` are therefore covered. Free-text `message`/`msg` content is never used to guess size.

A recognized size failure is never retried unchanged. Multi-question provider rejections may be split to distinguish aggregate pressure; singleton failures enter bounded compaction and create a per-file scheduling hint so sibling checks do not repeat the same oversized state. Exact rejected request bodies are never resubmitted.

An unknown HTTP 400/422 is different: it becomes a request-local `RequestRejectedError`. The affected checks are marked incomplete, one sanitized diagnostic is emitted for that file/signature, and unrelated requests continue. Diagnostics retain only status, safe machine fields, sanitized request ID, local request size/identity and a truncated SHA-256 response fingerprint; provider message/body text and submitted source are not retained. The shared client counts equivalent request-local failures. On the third equivalent rejection it raises a scan-fatal circuit-breaker error, preventing a systemic schema/API failure from generating thousands of doomed requests. Authentication/permission failures and other systemic transport/contract failures remain immediately fatal.

## Bounded request recovery

`planning.py` owns model-aware admission and shared-state packing. `execution.py` owns attempts/recovery for one active file. `compaction.py` owns local source selection. `client.py` owns HTTP classification and the scan-wide request-rejection breaker. None duplicates HTTP pacing or cache ownership.

For each unit check, at most `max_rounds` compacted attempts are made with decreasing recovery token estimates and strictly decreasing serialized bytes. After that, the complete target alone may be tried once when it satisfies the active bounds and has not already been rejected. Whole-file targets are never converted into partial-file scores. There is no averaging of fragment scores or generated summary substituted for source.

## AST evidence recipe

`syntax.py` extracts serializable source spans alongside the native parser's unit/reference data. No native node or executable source crosses into the evaluator. `LocalContext` builds a bounded recipe from:

- the exact complete target, which is mandatory;
- affordable enclosing lexical headers, with a bounded signature outline labelled as metadata rather than implementations;
- referenced same-file definitions and declarations, with at most two lexical-reference hops;
- relevant owner state, constructor candidates, and imports.

The recipe is a lexical heuristic, not binding/type/data-flow resolution. Same-name candidates can be unrelated; aliases, dynamically registered callbacks, macros, side-effect-only code and external definitions can be missed. Candidate count/hop/outline bounds are disclosed, not described as complete dependency coverage. Ordinary owner requests retain bounded complete import declarations as additional evidence; recovery may omit them if they do not fit.

Candidate bodies are never cut at arbitrary byte offsets. Inclusion uses exact AST spans. Gaps remain explicit original-file omissions. Lexical headers and source fragments need not form a standalone compilable program; they are documents with original locations. The target is always a contiguous complete span. `Evidence` identity hashes all encoded documents/coverage rather than their outer bounding interval, preventing different selections from sharing an identity accidentally.

When the syntax-derived candidates fit, no semantic selection call is made. When they do not, optional independent relevance Nouls order them. The questions include the active YAML question type, instructions, criteria and compact target locator via the shared `Check.auxiliary` builder; exact IDs and byte ranges remain local attribution metadata. Custom rule names, renamed sets and all supported primitive types use the same semantic binding. There is no hard-coded built-in interpretation or an assumption that a rule ID explains its meaning to the model.

`selection.py` is shared with enrichment. Questions are independently packed under the recovery budget. A bounded per-file prediction allowance limits extra work; valid results from completed selection batches remain usable if a later batch hits a size/budget stop. Unscored blocks retain deterministic syntax order. Selection does not certify omitted code irrelevant. Complete candidate spans are admitted until the budget is exhausted; the final assessment receives source and coverage, not prior verdicts or relevance scores. Ordinary transport/validation errors remain real failures rather than a silent deterministic fallback.

## Reporting and naming

Normal and verbose text display one file coverage line instead of hundreds of routine per-target reduction diagnostics. A coverage event with `aborted: true` is rendered as `scan aborted`, not as fake compaction; files with zero compacted targets say only that checks were omitted. Request-local provider rejections produce sanitized visible API diagnostics while allowing unrelated work to continue. JSON/JSONL retain detailed diagnostics, per-rule source coverage and `context_selection` audits, including selected candidate provenance, budgets, raw selection predictions, skipped candidates, recognized size failures, request-local rejection metadata and terminal outcome. Reduced requested context and omitted checks still produce incomplete status and exit 2; aggregation is presentation, not suppression of coverage loss.

`display_name` is separate from canonical lexical names and byte-span IDs. Argument closures receive syntax-derived call roles, with literal test/suite names where available. `then[arg2]` states an argument position, not proof of Promise behavior. Duplicate display labels remain disambiguated by source location and canonical ID. Rendering continues to escape terminal controls. No callbacks are dropped from analysis because their names are anonymous.

## What this release does not establish

MockTransport tests establish model-aware preflight, batch splitting versus context compaction, finite recovery, request-local rejection isolation/circuit breaking, precise attribution, error discrimination and evidence invariants. They do not measure Jev's real long-context reliability or whether a relevance ordering improves precision/recall. The TypeScript test patterns are adapted from the reported project; this is not a fresh live evaluation of that entire repository. The existing native-parser syntax failure policy remains strict, and generated source is not automatically excluded.

For live acceptance, save JSONL from a fixed source revision and pinned model. Inspect whether intact requests succeed, whether observed size failures match supported shapes, which source was selected/omitted, and whether the resulting judgments are correct. Lower diagnostic volume or more enrichment calls alone is not success.

## RC7 batching and concurrency changes

rc7 keeps sibling questions sharing an oversized owner together until recovery rather than letting the initial packing pass fragment them into singletons. Recovery still selects evidence per rule/target, but any checks that converge on the same exact encoded evidence are repacked before transport. This preserves target attribution and complete-target guarantees while avoiding repeated source tokens where the prepared state is actually identical.

Independent prepared batches from the same file may now be in flight concurrently. The HTTP client owns one scan-wide semaphore and one rate limiter, so increasing file-local readiness does not multiply configured transport concurrency or requests-per-minute. Parser subprocesses ignore SIGINT so the parent can produce one controlled incomplete report on Ctrl-C instead of child traceback noise.

The 3,000-line default `scan.max_full_file_lines` is separate from token recovery. It prevents complete-file semantic requests above the configured policy size and reports those checks as omitted. It does not truncate a file-level target into a fragment score.
