# Rule-aware context recovery — 0.2.0rc5

## Findings from the research

Sources reviewed on 2026-09-19:

- [TypeSafe's official agent guidance](https://github.com/typesafe-ai/skills/blob/main/skills/typesafe-ai/SKILL.md) distinguishes state from the question, requires complete meaning in instructions (question IDs are not model instructions), recommends independent typed judgments for multiple candidates, and composes fresh requests after acquiring evidence.
- The [official Python SDK error handling](https://github.com/typesafe-ai/typesafe-sdk-python/blob/main/src/typesafe_sdk/_core/errors.py) retains HTTP status/body and `x-typesafe-request-id`. Its [generated API models](https://github.com/typesafe-ai/typesafe-sdk-python/blob/main/src/typesafe_sdk/_schemas/models.py) define generic validation envelopes, not an explicit specialized context-limit response.
- The [Pydantic TypeSafe integration documentation](https://pydantic.dev/docs/ai/models/typesafe/) names `max_tokens_exceeded` and describes Jev-1.13 limits as 32k for state plus the longest question and 64k for state plus all questions. This is integration documentation, not an authenticated capture of TypeSafe's wire response or a permanent quota for every model/account.
- [Hierarchical Context Pruning (AAAI 2025)](https://ojs.aaai.org/index.php/AAAI/article/view/34782) investigates topology and selective source inclusion for repository-level code completion. It supports considering syntax/dependencies rather than raw concatenation. Its results concern completion models, **not** Jev's code-review accuracy; we do not transfer its accuracy claims to this scanner.

Direct live documentation/API schema pages were attempted but were not accessible through the research tools. No authenticated Jev call was made, and no production rejection body was captured. The implementation deliberately distinguishes documented machine-code guidance, compatible envelopes covered by tests, and application recovery policy.

A previous suggestion that a 46k estimated-token file should simply fit was too strong: it may exceed Jev-1.13's documented per-question window, and the local estimate is not its tokenizer. The improvement is to stop treating our estimate as authoritative while retaining bounded requests and honest recovery—not to promise a larger model window.

## Full source first, under explicit hard limits

The default estimated-token caps are now null. The 1 MiB serialized request ceiling, 64-question cap, source-file limits, pacing, authentication restrictions and finite transient retries remain. Users can still set numeric local token caps. Requested complete file/owner evidence is submitted when admitted by those local policies.

A size response is not treated as a transient retry of the same body. HTTP 413 is recognized directly. At HTTP 400/422 only the exact `max_tokens_exceeded` machine value is accepted, in top-level `code`, `error.code`, `detail.code`, or a complete `error`/`detail` string. For example, the tests exercise:

```json
{"error": {"code": "max_tokens_exceeded"}}
```

That is a **compatibility fixture**, not a claimed captured provider response. Other 400/422 errors, auth failures, malformed successful answers and generic messages containing the word “tokens” do not enter size recovery. Response text may echo submitted source and is not placed in exceptions or report diagnostics. The audit stores only status, recognized code, safe request ID and local request identity/size.

## Bounded request recovery

`planning.py` creates shared-state requests. `execution.py` owns attempts and recovery for one active file. It does not duplicate HTTP pacing or cache logic.

A failed multi-question batch is tested with a short singleton and smaller remaining batches. This distinguishes a possible aggregate-question limit from state that also fails alone. Answers are attributed and emitted once. If a singleton fails, a per-file state/serialized-question-size hint can send related unit checks straight to compaction instead of repeatedly transmitting the same large source. Byte length is not exact token count: this is conservative scheduling, not a proof that every sibling question would fail. Every whole-file question still receives its own attempt unless a user-configured hard limit forbids it. The hint is discarded after the file finishes and is not reused across models/accounts/runs.

For each unit check, at most `max_rounds` compacted attempts are made, with decreasing recovery token estimates and strictly decreasing serialized bytes. After that, the complete target alone may be tried once under the user's hard limits. This prevents a conservative recovery estimate from being mistaken for proof that the target is impossible. Exact already-rejected request bytes are never replayed within the executor. `oversized_context: skip` prohibits all context reduction.

Whole-file targets are never converted into partial-file scores. A rejected singleton file judgment is an explicit omission. A large class/impl that is itself the target is likewise not silently reduced to selected methods. There is no averaging of fragment scores or generated summary substituted for source.

## AST evidence recipe

`syntax.py` extracts serializable source spans alongside the native parser's unit/reference data. No native node or executable source crosses into the evaluator. `LocalContext` builds a bounded recipe from:

- the exact complete target, which is mandatory;
- affordable enclosing lexical headers, with a bounded signature outline labelled as metadata rather than implementations;
- referenced same-file definitions and declarations, with at most two lexical-reference hops;
- relevant owner state, constructor candidates, and imports.

The recipe is a lexical heuristic, not binding/type/data-flow resolution. Same-name candidates can be unrelated; aliases, dynamically registered callbacks, macros, side-effect-only code and external definitions can be missed. Candidate count/hop/outline bounds are disclosed, not described as complete dependency coverage. Ordinary owner requests retain bounded complete import declarations as additional evidence; recovery may omit them if they do not fit.

Candidate bodies are never cut at arbitrary byte offsets. Inclusion uses exact AST spans. Gaps remain explicit original-file omissions. Lexical headers and source fragments need not form a standalone compilable program; they are documents with original locations. The target is always a contiguous complete span. `Evidence` identity hashes all encoded documents/coverage rather than their outer bounding interval, preventing different selections from sharing an identity accidentally.

When the syntax-derived candidates fit, no semantic selection call is made. When they do not, optional independent relevance Nouls order them. The questions include the active YAML question type, instructions, criteria and exact target via the shared `Check.auxiliary` builder. Custom rule names, renamed sets and all supported primitive types use the same semantic binding. There is no hard-coded JEV04 interpretation or an assumption that a rule ID explains its meaning to the model.

`selection.py` is shared with enrichment. Questions are independently packed under the recovery budget. A bounded per-file prediction allowance limits extra work; valid results from completed selection batches remain usable if a later batch hits a size/budget stop. Unscored blocks retain deterministic syntax order. Selection does not certify omitted code irrelevant. Complete candidate spans are admitted until the budget is exhausted; the final assessment receives source and coverage, not prior verdicts or relevance scores. Ordinary transport/validation errors remain real failures rather than a silent deterministic fallback.

## Reporting and naming

Normal and verbose text display one file coverage line instead of hundreds of routine per-target reduction diagnostics. Actual parser, read and API failures remain immediately visible. JSON/JSONL retain detailed diagnostics, per-rule source coverage and `context_selection` audits, including selected candidate provenance, budgets, raw selection predictions, skipped candidates, failed-request metadata and terminal outcome. Reduced requested context and omitted checks still produce incomplete status and exit 2; aggregation is presentation, not suppression of coverage loss.

`display_name` is separate from canonical lexical names and byte-span IDs. Argument closures receive syntax-derived call roles, with literal test/suite names where available. `then[arg2]` states an argument position, not proof of Promise behavior. Duplicate display labels remain disambiguated by source location and canonical ID. Rendering continues to escape terminal controls. No callbacks are dropped from analysis because their names are anonymous.

## What this release does not establish

MockTransport tests establish request construction, finite recovery, precise attribution, error discrimination and evidence invariants. They do not measure Jev's real long-context reliability or whether a relevance ordering improves precision/recall. The TypeScript test patterns are adapted from the reported project; this is not a fresh live evaluation of that entire repository. The existing native-parser syntax failure policy remains strict, and generated source is not automatically excluded.

For live acceptance, save JSONL from a fixed source revision and pinned model. Inspect whether intact requests succeed, whether observed size failures match supported shapes, which source was selected/omitted, and whether the resulting judgments are correct. Lower diagnostic volume or more enrichment calls alone is not success.
