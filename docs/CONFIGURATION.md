# Configuration and rule contracts — version 3

Generate a complete project configuration with `jevscan --init-config`. `--show-config` prints the resolved settings; `--list-rules` shows target/context and both severity levels.

## Inheritance and discovery

An explicit `--config` wins. Otherwise discovery checks the working directory upward, stopping at a Git worktree boundary, then the supplied targets. Conflicting target configurations or both `jevscan.yaml` and `jevscan.yml` at one level are errors. The selected configuration applies to the whole invocation, not separately to nested directories.

Without `extends: default`, a project file replaces the entire rule set. Omitted operational settings receive schema defaults. With `extends: default`, mappings merge recursively and lists replace; use `enabled: false` to disable one inherited rule. Duplicate keys, unknown fields, invalid levels, and unsupported target scopes fail before any API request.

```yaml
version: 3
extends: default
rules:
  mixed-responsibilities:
    context: file
    report:
      levels:
        warning:
          min_probability: 0.65
```

## What is being judged?

Each rule has independent `target` and `context` settings:

| Setting | Values and meaning |
| --- | --- |
| `target: unit` | A complete extracted function, method, class, impl, or other lexical unit |
| `target: file` | The complete parsed file, including top-level statements |
| `context: unit` | Just the target source, plus bounded import snippets |
| `context: owner` | The target's lexical owner, or file for a top-level callable |
| `context: file` | The containing file |

Unit rules require nonempty `applies_to`. Supported kinds are `function`, `method`, `closure`, `class`, `struct`, `enum`, `trait`, `impl`, `interface`, `type`, `module`, and `package`. These are **lexical kinds**, not additional evaluation scopes. `target: module` and `target: tree` are not implemented and are rejected.

`target` defaults to `unit`; its context defaults to `owner`. A file rule defaults to `context: file` and must not specify `applies_to`, `require_body: true`, or a narrower context. Unit rules can require an implementation with `require_body: true`. All rules default to enabled and all five supported languages; TSX uses `typescript`, JSX uses `javascript`.

An owner-level target sees itself under `context: owner`. A method sees its class/impl; a nested function sees its enclosing callable. Rust owner context starts with the full file because type definitions and impls are siblings; this includes same-file evidence but does not resolve matching types, traits, imports, or cross-file impls. On a size reduction it falls back to the lexical impl and then the complete method.

Every question explicitly identifies the target and defines `source` in the YAML instructions/criteria as **that target**, not everything included in the request. Related targets may share one evidence document and API request. Shared evidence does not combine their scores.

## Three question forms

### Noul: an affirmative proposition

```yaml
version: 3
rules:
  mixed-work:
    target: unit
    context: owner
    applies_to: [function, method, closure]
    require_body: true
    question:
      type: noul
      instructions: >-
        Does source interleave unrelated responsibilities in a way that obscures
        its main operation? Necessary domain complexity alone is not a problem.
    report:
      message: Review the responsibilities combined in this operation.
      levels:
        warning: {min_probability: 0.50}
        error: {min_probability: 0.90}
```

`noul` is the provider's value for the affirmative proposition, in `[0,1]`. `report.expected: false` makes reporting compare `1 - noul` instead. The raw value displayed remains `noul`. There is no separate Noul confidence field/gate. Noul cannot use score or choice fields.

### Choice: named alternatives, including uncertainty

```yaml
question:
  type: choice
  instructions: Which statement about the validation in source is supported?
  criteria:
    redundant: The supplied evidence establishes the same invariant before a repeated check.
    justified: The check protects a trust boundary or changed state, or no repeated check exists.
    unknown: A necessary upstream guarantee cannot be established from the supplied code.
report:
  message: A check appears to repeat an established invariant.
  choices: [redundant]
  uncertain_choices: [unknown]
  levels:
    warning: {min_probability: 0.60, min_confidence: 0.50}
    error: {min_probability: 0.92, min_confidence: 0.70}
```

The selected label must be one of `report.choices`, its selected-label probability must meet the level's `min_probability`, and its confidence must meet `min_confidence` when specified. All conditions are ANDed. `uncertain_choices` is optional, must contain valid criteria labels, and must not overlap defect choices. Such a label produces `unknown`, never a finding.

Below-threshold confidence, weak support for a non-defect label, or a selected defect label that does not satisfy a reporting gate also produces `unknown`. Uncertainty is not changed to green simply because no finding passed. It is counted in the summary and shown with `?` under `-v`; it does not by itself fail the scan.

### Score: ordered criteria, not a probability

```yaml
question:
  type: score
  instructions: How difficult is the main execution path through source to follow?
  criteria:
    - Clear execution path, including necessary domain branching.
    - Mostly clear, with some local complexity.
    - Interleaved concerns or nesting substantially obscure the operation.
    - Tangled control flow makes important transitions difficult to establish.
report:
  message: Review this operation's control flow.
  levels:
    warning: {min_score: 1.0, min_confidence: 0.60}
    error: {min_score: 2.0, min_confidence: 0.70}
```

Criteria correspond to `0, 1, ...`; returned scores may be fractional. Both levels must use the same direction: `min_score` for larger-is-worse, `max_score` for smaller-is-worse. The valid range is zero through the final criterion index. Score rules cannot use probability gates, choice lists, or `expected: false`. A low-confidence non-finding is `unknown`, not confidently OK.

## Levels, messages, and output

Both `report.levels.warning` and `report.levels.error` are required. Thresholds are inclusive. After any configured Noul uncertainty band is applied, the error gate is checked first and must be at least as strict as the warning gate; `min_confidence` participates in this ordering. Otherwise the result is OK or uncertain as described above. YAML stores semantic levels, not ANSI color escape codes.

`report.message` is the configurable explanation printed below a warning/error. The same message is used for both levels; it is not generated repair advice and does not localize an issue more narrowly than the target.

| Outcome | Display | Default text report |
| --- | --- | --- |
| OK | Green `·` | Hidden |
| Warning | Yellow `!` | Shown |
| Error | Red `x` | Shown |
| Unknown | Cyan `?` | Hidden; counted in summary |

`-v` / `--verbose` shows all evaluated answers. Operational/coverage diagnostics remain visible in either mode. `--max-display` counts visible targets, not individual checks; `0` means unlimited. JSON/JSONL always preserve all answers and omissions. Offline inventory always lists extracted units.

Packaged defaults retain the earlier warning/error numeric boundaries:

| Rule | Target / context | Warning | Error |
| --- | --- | --- | --- |
| mixed-responsibilities | unit / owner | noul ≥ .50 | noul ≥ .90 |
| mixed-abstraction-levels | unit / owner | noul ≥ .50 | noul ≥ .90 |
| unclear-control-flow | unit / owner | score ≥ 1, conf ≥ .60 | score ≥ 2, conf ≥ .70 |
| redundant-validation | unit / file | defect p ≥ .60, conf ≥ .50 | defect p ≥ .92, conf ≥ .70 |
| hidden-invariant-failure | unit / file | defect p ≥ .60, conf ≥ .50 | defect p ≥ .92, conf ≥ .70 |
| unhelpful-decomposition | unit / file | defect p ≥ .60, conf ≥ .50 | defect p ≥ .90, conf ≥ .70 |
| incohesive-owner | unit / file | noul ≥ .50 | noul ≥ .90 |
| fragmented-ownership | file / file | noul ≥ .50 | noul ≥ .92 |
| duplicated-behavior | file / file | noul ≥ .50 | noul ≥ .93 |

These are review-policy starting points, not measured correctness probabilities or thresholds calibrated across projects. Richer evidence and a changed target can change Jev's answers; the earlier unit-only self-scan is not a benchmark for file-level judgments.

## Request planning and resource limits

```yaml
evaluation:
  max_context_tokens: 28000  # estimated state + longest question, including reserve
  max_total_tokens: 56000    # estimated state + all questions, including reserve
  token_reserve: 512
  bytes_per_token: 3.0       # heuristic over serialized UTF-8 input
  max_request_bytes: 1048576 # exact serialized request-byte ceiling
  max_questions: 64
  oversized_context: reduce # reduce | skip
```

The estimator is deliberately visible and configurable. It is **not TypeSafe's tokenizer** and cannot guarantee that every accepted local plan fits the provider. Body bytes include JSON escaping, target metadata, instructions, criteria, question IDs, and model information. The state is counted once, not once per question.

Question packing retains the same evidence. A provider HTTP 413 or a structured `max_tokens_exceeded` error at HTTP 400/422 also permits deterministic, bounded recovery: bisect questions, then try a smaller context for an individual check. An unrelated 400/401 or an invalid answer is not permission to change the evidence or fabricate a result.

`reduce` chooses the broadest configured envelope that fits, then lexical owner, then unit where applicable. Requested-context reduction records exact ranges, emits a diagnostic, and makes the scan incomplete even though the target itself remains complete. A non-finding based on reduced requested context is classified unknown. `skip` requires the original requested envelope unchanged.

The target is **never truncated**. File targets have no smaller whole-file alternative. An oversized file/class may be omitted while its fitting methods are still evaluated. There is no silent chunk scoring, max/mean aggregation, or whole-file result inferred from partial text. The report's `target_complete` and `context_complete` fields distinguish these cases.

| Group | Other settings |
| --- | --- |
| `scan` | `include`, `exclude`, `respect_gitignore`, `jobs`, `batch_size`, `queue_size`, `max_file_bytes`, `max_units_per_file` |
| `jev` | `model`, `concurrency`, `requests_per_minute`, `timeout_seconds`, `retries`, `max_retry_delay` |
| `cache` | `enabled`, project-relative `path`, `ttl_seconds` (`0` means no expiration) |

`scan.max_file_bytes` protects filesystem reads, not model quality; default 2,000,000 bytes. `queue_size` now counts parsed files awaiting evaluation (default 8), not individual units. Workers retain complete active files and their envelopes/results, so raising file size, nesting, rule count, and concurrency together can use substantial memory. `jev.concurrency` limits simultaneous file evaluators; batches within one file execute sequentially. `requests_per_minute` paces every HTTP attempt, including retries and size recovery.

Include/exclude use Git-style patterns, not `Path.glob`: `*.py` matches that basename at any depth; leading `/` anchors at the project root. Ignored parent directories are pruned before traversal. Authentication and endpoint selection are environment-only, not project YAML.

## Migration from version 2

1. Change `version: 2` to `version: 3` (or regenerate with `--init-config` into a **new** path and transfer overrides).
2. Remove `scan.max_unit_bytes`, `scan.context_bytes`, and `scan.context_members`. Source selection is controlled by each rule's `context`; request sizing is controlled by `evaluation`.
3. Move `jev.max_request_bytes` and `jev.max_questions` to `evaluation` when previously configured. Reconsider their values: the old 60,000-byte request cap defeats richer context.
4. Keep existing `report.levels.warning/error`; add `target/context` as needed. A standalone unit rule defaults to owner context. File rules must remove `applies_to` and `require_body` and use file context.
5. Review inherited file-level defaults: `duplicated-behavior` and `fragmented-ownership` now apply once per file, not once per owner. Add `uncertain_choices` to custom Choice rules with an unknown category.
6. Consumers of machine reports must accept **report schema 3** (schema 2 introduced the target fields; RC2 adds the audit/status changes below): evaluations and findings contain `target`, not `unit`; evaluations add `statuses`, `evidence`, `models`, `scales`, `cached_rules`, and `skipped_rules`. Offline `unit` inventory events remain lexical metadata.

Version-1 configurations first need their single severity/threshold moved into explicit warning/error levels. Old-version configurations are rejected before scanning. Cache entries are versioned and the new request shape invalidates prior unit-only answers; subsequent reporting-only changes can reuse the new raw-answer cache.


## RC2: bounded evidence enrichment (configuration version remains 3)

These fields are additive; existing v3 configurations load without migration. Enrichment is enabled by the operational default, including standalone configurations. `--no-enrichment` overrides YAML for that invocation. `--show-config` prints the effective settings.

```yaml
version: 3
extends: default

enrichment:
  enabled: true
  max_checks_per_file: 12
  max_calls_per_file: 36
  max_candidates: 12
  max_evidence: 3
  min_route_probability: 0.70
  min_route_confidence: 0.50
  min_relevance: 0.65
  max_source_files: 1000
  max_source_bytes: 16777216

rules:
  redundant-validation:
    enrich: true
  unclear-control-flow:
    enrich: false
  mixed-responsibilities:
    report:
      uncertain_range: [0.40, 0.60]
```

There is one enrichment pass, not a configurable recursive state machine. `max_calls_per_file` counts routing, candidate-selection and reassessment predictions, including cache hits. HTTP retry attempts are separately controlled by `jev.retries` and the global rate limiter. Reaching a review/call limit retains uncertainty; it is not a successful reassessment. `max_evidence` cannot exceed `max_candidates`. Route probability and candidate relevance thresholds must be greater than 0.5. These defaults are heuristics awaiting domain calibration.

`max_source_files` and `max_source_bytes` bound one lazy source index for the invocation, not each target. Reads over a configured file/total byte budget are rejected, not partially analyzed. One overflow byte may be read. Unreadable or unsupported source, budget exhaustion, and candidate truncation are recorded as partial retrieval coverage. The scan include/exclude and Git-ignore filters apply to this discovery too. A lexical name match is only a candidate; no complete call graph is implied even when discovery finishes.

**Review data-sharing scope before live use.** The evidence search spans the resolved project root, not only command-line target paths. Tests outside `src/` may be supplied to Jev. To restrict sharing to primary requested evidence, disable enrichment. API authentication/endpoint cannot be redirected by repository YAML. No model-provided path is opened.

### Applicability and uncertainty

`require_body: true` now requires a body containing an implementation, not merely syntax such as `pass` or `...`. Declaration-only methods remain present in offline inventory. An empty block is not an implementation for these checks; a real expression-bodied closure is. `require_members: true` requires at least one directly owned implemented callable. The packaged decomposition rule uses this filter; it is optional for custom unit rules and invalid for file rules.

Choice rules may set `report.not_applicable_choices` to labels in their criteria. These labels must be disjoint from defect `choices` and `uncertain_choices`. A sufficiently supported not-applicable answer has status `not_applicable`, not `ok`. A confident routing judgment may also reach this status; the raw primary answer and routing prediction are retained so this inference is auditable.

Noul rules may set `report.uncertain_range: [lower, upper]`, which must enclose 0.5. The interval is **lower-inclusive, upper-exclusive**, applies to raw `noul` (also for `expected: false`), and takes precedence over reporting levels. `null` disables the uncertainty band. Thus the packaged `[0.40, 0.60)` leaves 0.599 uncertain and lets 0.600 reach the ordinary warning gate. Confidence-based Choice/Score rules retain their existing thresholds; uncertainty is now given a separate reason rather than being conflated with severity.

Standalone/copied v3 rules do not inherit the new Noul band or decomposition filter. With `extends: default` they do. The numerical warning/error gates are unchanged, but the band deliberately changes handling around the Noul warning boundary.

### Machine report schema 3

RC2 adds `not_applicable` to statuses, plus per-rule `uncertainty_reasons` and `reviews`. A review records its trigger, original answer/model/evidence, each prediction's request hash/model/cache/raw answer, candidate identities/relevance, retrieval limits, selected source hashes/ranges, and stop outcome. Final `answers`, `findings`, `evidence`, and summary counts describe the final assessment only; earlier judgments do not count twice. `cached_rules` and the target `cached` marker require every contributing prediction to be cached.

`summary.enrichment_calls` includes cached predictions; `summary.requests` counts actual HTTP attempts across primary and auxiliary work. `enrichment_resolved` includes transitions from unknown to OK, warning, error, or not-applicable; it is a workflow count, not measured model accuracy. Provider context rejection in enrichment preserves the earlier uncertain answer with a specific stop reason. Other API, cache, and validation errors remain operational failures and make the scan incomplete.
