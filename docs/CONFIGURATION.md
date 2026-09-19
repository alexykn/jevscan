# Configuration

## Discovery and precedence

Use `jevscan.yaml` or `jevscan.yml` with `version: 4` (version 4 is also the default). PyYAML uses a SafeLoader that rejects duplicate keys and non-string mapping keys; `--show-config` uses safe YAML serialization. Input is limited to 1 MiB. Invalid/duplicate YAML keys, duplicate rule names, unknown settings/selectors, and invalid rule contracts fail before scanning.

Resolution is explicit `--config`; otherwise the nearest config above the working directory; then the targets' nearest config; finally the packaged defaults. Upward discovery stops at a Git boundary. One config applies to the invocation; targets with different configs require separate scans or an explicit config. A selected config's directory is the project root. Without one, the nearest `.git`, `pyproject.toml`, `Cargo.toml`, or `package.json` determines the root.

Both existing YAML filenames are supported. If both occur in one directory, discovery reports an ambiguity; an explicit `--config` chooses one. Explicit configuration paths must end in `.yaml` or `.yml`. Empty or comment-only YAML adds nothing. Older schema versions fail with a migration message, without changing the file format. Source API-key/origin configuration remains environment-only.

`--init-config` writes a minimal additive file without overwriting. `--show-config` writes all resolved definitions and selection policy as YAML that can be read back. The latter is a snapshot of current defaults; a small override is usually easier to maintain across upgrades.

## Additive rule catalogue

The packaged rules are **always loaded**. Project settings merge into them: mappings recursively merge, lists replace, and the `rules` array is merged by `name`, not by array position. `rules: []` adds nothing; it does not delete defaults. There is no `extends` field.

Each rule's `name` is its stable unique identity. `title` is a human-readable description; `ruleset` names its group. Rule and ruleset names must start with an ASCII letter and contain at most 64 letters/digits/hyphens/underscores. They are case-sensitive and cannot overlap. `ALL` is a reserved selector. Names need not follow a numeric convention, but built-ins use JEV01–JEV09.

| Name | Title |
|---|---|
| JEV01 | mixed-responsibilities |
| JEV02 | unclear-control-flow |
| JEV03 | mixed-abstraction-levels |
| JEV04 | redundant-validation |
| JEV05 | hidden-invariant-failure |
| JEV06 | unhelpful-decomposition |
| JEV07 | fragmented-ownership |
| JEV08 | incohesive-owner |
| JEV09 | duplicated-behavior |

The `JEV` ruleset contains built-ins. An initially empty `project` ruleset is available for custom rules that omit `ruleset`. Declare other sets using `rulesets.NAME`; `description` is optional and `enabled` defaults to true.

```yaml
version: 4
lint:
  ignore: [JEV09]
rulesets:
  TEAM:
    description: Team conventions
rules:
  - name: TEAM01
    title: mixed-responsibilities-at-boundaries
    ruleset: TEAM
    applies_to: [function, method]
    require_body: true
    context: file
    question:
      type: noul
      instructions: Does the target mix transport handling with business policy in a way that obscures either?
    report:
      message: Review the boundary between transport and business policy.
      uncertain_range: [0.4, 0.6]
      levels:
        warning:
          min_probability: 0.75
        error:
          min_probability: 0.95
  - name: JEV02
    report:
      levels:
        warning:
          min_score: 1.2
```

This keeps all built-ins except ignored JEV09, adds TEAM01, and overrides only JEV02's warning score. Both rule and ruleset `enabled: false` are hard disables; neither is silently undone by selection. New rules need complete question/report contracts. Existing names are intentional overrides; duplicate names within the same project document are errors. Use YAML `null` to clear an inherited optional field, such as `uncertain_range: null`. Required fields cannot be cleared. The resolved YAML retains nulls so reloading it does not accidentally restore cleared defaults. A new rule is often clearer than changing the primitive of an existing rule, which requires replacing all incompatible inherited fields.

## Select and ignore

`lint.select: [ALL]` is the default: all enabled built-in **and custom** rules are eligible. An explicit `select` replaces that baseline. `ignore` removes matching rules. Selectors are exact rule names, exact ruleset names, or `ALL`; there is no prefix or glob interpretation.

```yaml
lint:
  select: [TEAM, JEV02]
  ignore: [TEAM02]
```

Effective selection is: enabled rule, enabled ruleset, matched by `select`, and not matched by `ignore`. Ignore wins even over an exact rule selection. `select: []` or `ignore: [ALL]` selects nothing; a live scan then errors instead of reporting everything clean. Offline inventory remains available.

`--select NAME` (alias `--rule`) is repeatable and replaces configured `select`. Repeatable `--ignore NAME` adds to configured ignores. Use `--list-rules` to see effective enablement, including suppressed definitions. Rules are selected before question creation: ignored rules are not sent to Jev and do not become findings.

## Rule target and evidence

`target: unit` (default) or `"file"` determines attribution. Unit rules require nonempty `applies_to`; file rules require file context, no `applies_to`, and no body/member requirements. `module` and `tree` are not supported.

`context` is `unit`, `owner`, or `file`. Defaults are owner for units and file for files. Owner context uses the lexical owner, or the file for top-level functions; Rust owner context starts with the file. `languages` defaults to the five supported languages. `require_body: true` excludes declarations and obvious stub implementations. `require_members: true` requires implemented callable members for owner-level checks. No heuristic claims compiler-level call resolution.

`applicability` is optional. It can declare `requires_any` and/or `requires_all` from the exact syntax-fact vocabulary
`validation_candidate`, `fallback_candidate`, `helper_relationship`, and `executable_behavior`. An absent policy never
adds a deterministic gate. A target missing a declared prerequisite is counted as not applicable and recorded separately
from transport omissions with the declared fact in its machine-readable reason. Validation and fallback facts are broad
admission signals over call, predicate, and control-flow syntax; they do not classify that syntax semantically.
`helper_relationship` requires a visible lexical call between implemented siblings. The schema rejects empty, duplicate,
or overlapping fact lists. Built-ins and custom rules use the same evaluator.

`enrich` defaults to true. `enrich_on` is a replaceable list of triggers. Packaged defaults admit missing/reduced evidence; JEV06 also admits applicability. Available values are `missing_evidence`, `reduced_context`, `applicability`, `low_confidence`, `low_choice_probability`, `weak_defect_signal`, and `probability_ambiguous`. The first two have priority across a file. Additional triggers should be enabled only when evidence may help that rule; ordinary ambiguity is not evidence of missing code. `enrichment_families` accepts `callers`, `callees`, `tests`, and `enclosing_context`.

`targeted_enrichment` is optional and contains `when_choices` and/or `when_reasons`. When it matches the raw Choice
label or admitted `enrich_on` trigger, code retrieves only the declared evidence families, batches candidate relevance questions
over shared candidate state where budgets allow, and reassesses once only after useful evidence is admitted. Rules
without this policy retain generic disposition-and-family routing, including custom rules.

## Questions and reporting

All rules need `question.type`, focused `question.instructions`, `report.message`, and both `report.levels.warning` and `.error`. Instructions and criteria describe the target; supplied surrounding source is evidence. Rule IDs are local identities, not substitutes for complete instructions.

### Noul

Noul is a scalar probability of a proposition, not intensity. Native TypeSafe criteria are an optional object with
explicit `"true"` and `"false"` descriptions; built-in Nouls provide both. Levels use `min_probability` and no
separate confidence. `report.expected: false` tests `1 - noul`. Optional `uncertain_range: [0.4, 0.6]` applies to raw
Noul with an inclusive lower/exclusive upper boundary. It must enclose 0.5. A signal inside that band can still be
displayed as an uncertain warning/error when it crosses the directional reporting threshold.

### Choice

Choice uses named criteria and reports only selected defect labels listed in `report.choices`. Both levels require `min_probability`; `min_confidence` is optional. `uncertain_choices` identifies missing-evidence answers; `not_applicable_choices` is separate. These categories must be disjoint and present in `question.criteria`.

```yaml
rules:
  - name: PROJECT02
    title: concealed-failure
    applies_to: [function, method]
    context: file
    require_body: true
    question:
      type: choice
      instructions: Does this operation silently conceal a violation of an explicitly established internal invariant?
      criteria:
        concealed: An established internal invariant is violated and silently concealed.
        legitimate: Only legitimate runtime conditions are handled, or no concealment is present.
        missing: A necessary invariant or behavior is absent from the evidence.
    report:
      message: Review the concealed invariant failure.
      choices: [concealed]
      uncertain_choices: [missing]
      levels:
        warning:
          min_probability: 0.6
          min_confidence: 0.5
        error:
          min_probability: 0.92
          min_confidence: 0.7
```

### Score

Score uses an ordered array of criterion descriptions and a zero-based numeric scale. Levels use exactly one of `min_score` or `max_score`, with the same direction at both levels, and optional `min_confidence`. Levels must lie inside the rubric. Score reports cannot use `min_probability`, Choice labels, or `expected: false`.

### Confirmation and tentative severity

The highest matching numeric signal gives severity; confidence determines confirmation at **that** level. An error-level result lacking error-level confidence is an uncertain error, not a confirmed warning. Confident outcomes create `findings`; uncertain above-threshold outcomes create `tentative_findings`. The latter remain unknown, appear by default with cyan `?`, and do not trigger `--fail-on`.

Benign Choice labels and missing-evidence labels never acquire an invented defect severity. Ordinary unknown results below reporting thresholds require `-v`. Per-rule messages and all numerical thresholds are customizable. Thresholds remain heuristic and are not measured correctness probabilities.

## Operational settings

| Section | Important defaults / meaning |
|---|---|
| scan | Existing five-language include patterns and dependency/build excludes; respect_gitignore=true; jobs=0 (up to 8 parsers); batch_size=8; queue_size=8; max_file_bytes=2000000; max_units_per_file=10000; max_full_file_lines=3000 |
| jev | model="jev-latest"; concurrency=16; requests_per_minute=600; timeout_seconds=30; retries=3; max_retry_delay=60 |
| evaluation | max_context_tokens=28000; max_total_tokens=56000; token_reserve=512; bytes_per_token=3.0; max_request_bytes=1048576; max_questions=64; oversized_context="reduce" |
| compaction | context_tokens=24000; max_rounds=3; max_candidates=32; max_calls_per_file=12; semantic=true |
| enrichment | enabled=true; mode="targeted"; max_checks_per_file=12; max_calls_per_file=36; max_candidates=12; max_evidence=3; max_source_files=1000; max_source_bytes=16777216 |
| budget | max_requests=1500; max_input_tokens=5000000; max_cost=null; input_cost_per_million=0.042 |
| cache | enabled=true; path=".jevscan-cache/results.sqlite3"; ttl_seconds=86400 |

Use `--show-config` for the complete resolved values, including source patterns. API pacing is a local budget, not an account quota claim. Context estimates are not a provider tokenizer. `oversized_context: skip` forbids reducing requested evidence. Targets are never truncated.

Routing uses `min_route_probability=0.70` and `min_route_confidence=0.50` for the **disposition Choice**, `min_evidence_probability=0.60` independently for each evidence-family Noul, and `min_relevance=0.65` for each candidate. Do not add or normalize independent family probabilities. A disposition stop wins over speculative family answers. One review can use several families, but the candidate/evidence/call budgets stay shared, not multiplied by family count.

The cache path must be relative and inside the project. Cache entries contain provider answers, not submitted source. rc7 also stores validated per-judgment entries keyed by endpoint, model, exact evidence, exact bound question, and prompt compatibility, so changing HTTP batch composition does not force an unchanged judgment to be purchased again. `--no-cache` disables both request and judgment cache use, including enrichment.

`enrichment.mode` is `off`, `targeted`, or `full`. In targeted mode each rule's `enrichment_families` list limits retrieval to relevant evidence families. `callees` is the user-facing name for syntax-linked referenced definitions; `callers`, `tests`, and `enclosing_context` are also available. `full` admits every family; `off` disables the enrichment pass.

`budget` guards paid request attempts before transport. `max_requests`, `max_input_tokens`, and `max_cost` are optional hard ceilings; `input_cost_per_million` gives the configurable input-price basis for cost estimates. A cost limit is expressed in the same currency/unit as that configured price. The CLI exposes `--max-requests`, `--max-input-tokens`, and `--max-cost`. Budget exhaustion is incomplete coverage, not a clean result. The packaged profile defaults to 1,500 request attempts and 5,000,000 estimated input tokens as a safety ceiling; set either to `null` in YAML to remove that guard.

`scan.max_full_file_lines` limits only analysis that requires a complete file state. A larger file produces an explicit coverage error for those checks while smaller owner/unit contexts can continue. This threshold is operational and does not by itself claim that the file is semantically defective. Set it to `null` to remove the line ceiling.

`--plan` performs parsing and initial packing without an API key or live inference. Its token/cost estimate is intentionally conservative and does not predict later compaction, enrichment, retries, or warm judgment-cache reuse. `--changed` scans Git working-tree/staged changes plus untracked files; `--staged` scans the index only. These modes restrict the selected files and do not claim that downstream dependents are unaffected.

CLI model/config operational overrides are validated by the same schema.

## Migration from YAML v3

1. Keep `jevscan.yaml` or `jevscan.yml`, set `version: 4`, and remove `extends`. YAML remains the configuration format.
2. Convert the rule mapping to a list under `rules`, with `name: JEVxx` on each built-in override. Use the ID table above to replace built-in slugs, including CLI `--rule` arguments and consumers of `Finding.rule`.
3. Keep existing question, reporting, and operational settings as YAML mappings. Threshold names and meanings do not change.
4. Built-ins now remain active automatically. For an old standalone custom-only config, declare your custom rules and set `select: [your-set]` under `lint` (or enumerate rule names). To disable all checks, use `select: []`, not an empty rule array.
5. For script consumers, report schema 5 retains confirmed/tentative separation but uses stable IDs and adds `rule_metadata`. Review routing now contains `disposition`, `evidence_families`, `evidence_probabilities`, and per-family retrieval coverage rather than one `route`.

Before:

```yaml
version: 3
extends: default
rules:
  duplicated-behavior:
    enabled: false
  unclear-control-flow:
    report:
      levels:
        warning:
          min_score: 1.2
```

After:

```yaml
version: 4
lint:
  ignore: [JEV09]
rules:
  - name: JEV02
    report:
      levels:
        warning:
          min_score: 1.2
```

There is no automatic rewrite or hidden legacy evaluator; the migration changes schema and selection semantics, not serialization. Migration errors are preferable to silently changing which checks a project runs. Numerical defaults were not retuned in rc4.


## RC6 model-aware context recovery (configuration stays YAML v4)

```yaml
evaluation:
  max_context_tokens: 28000
  max_total_tokens: 56000
  token_reserve: 512
  bytes_per_token: 3.0
  max_request_bytes: 1048576
  max_questions: 64
  oversized_context: reduce
compaction:
  context_tokens: 24000
  max_rounds: 3
  max_candidates: 32
  max_calls_per_file: 12
  semantic: true
```

For Jev 1.13, `RequestBudget` also knows the published provider ceilings: 32,000 tokens for state plus the longest question and 64,000 for state plus all questions. The packaged 28,000/56,000 values are conservative planning thresholds below those ceilings. `null` removes the additional local margin but **does not disable a known provider ceiling**; explicit values above a known ceiling are clamped to it. For an unknown model identifier, only configured token limits plus the byte/question bounds are available until the implementation gains a profile for that model.

The token estimate remains approximate. Successful Jev responses expose `usage.input_tokens`; rc6 shares a run-local calibration across file evaluators that may only make future byte/token estimates more conservative. It never raises a token ceiling. The calibration is ephemeral and is not persisted across runs, models or accounts.

Preflight uses the two token dimensions separately. If only aggregate/question-count/serialized-size packing is over budget and several questions share the same state, questions are split without reducing source. If state plus the longest question is over budget, unit evidence enters AST compaction before HTTP. If both are over budget, source is compacted and the surviving checks are repacked. Whole-file targets are never converted into fragment judgments. `oversized_context: skip` forbids compaction and produces explicit omissions instead.

`compaction.context_tokens` is a recovery target, not a model window. Subsequent attempts halve that target and reduce serialized bytes below the previous failed request. `max_rounds` bounds compacted attempts (1–8), `max_candidates` caps the syntax-derived local pool (1–128), and `max_calls_per_file` caps auxiliary selection requests, including cache hits (0–256). `semantic: false` or a zero call budget leaves deterministic AST selection enabled.

Provider size rejection remains a fallback for estimator/tokenizer mismatch. HTTP 413 and exact structured `max_tokens_exceeded` machine values at HTTP 400/422 enter size recovery; free-text token mentions do not. Unknown HTTP 400/422 responses become request-local omissions with sanitized machine fields (`code`, `type`, `status`, bounded scalar `error`), request ID and an irreversible response fingerprint. Response messages/bodies and submitted source are never copied into diagnostics. Three equivalent request-local rejections trip a scan-wide circuit breaker to avoid thousands of doomed requests. 401/403 and other systemic transport/contract failures remain scan-fatal.

Compaction selects source from the already parsed file only. It never silently expands sharing to other files. Ordinary enrichment still uses its separately documented root-level search and budgets. `--no-enrichment` therefore does not disable local preflight/recovery or its optional relevance judgments. Offline mode performs neither.

Auxiliary relevance instructions are assembled from `Check.auxiliary`: exact target metadata and the full active `rule.question`, including all criteria. Report thresholds/messages are not injected as semantic instructions, and neither an earlier verdict nor ranking scores enter the final assessment state. Changing a rule question or selected source changes cache identity. Compaction/recovery audits record source spans, budgets, candidate omissions, raw relevance answers, request hashes, request-local provider rejections and explicit stops.

Report schema **7** adds the request-rejection counter and request-local rejection metadata while preserving rc5's `target.display_name`, `context_selection`, file `coverage` events and size/compaction counters. Text coverage now distinguishes `scan aborted` from ordinary compaction/omission; a file with zero compacted targets is no longer described as compacted merely because unfinished checks exist. Reduced contexts and skipped targets still set incomplete status.

## RC7 cost-aware evidence batching (configuration stays YAML v4)

rc7 does not add a new question primitive. It uses System One's existing map of independent typed questions more aggressively: checks of different rules and targets are packed together whenever they share the exact evidence state. Noul, Choice, and Score questions may coexist in the same request. Source is never enlarged merely to create a batch.

When a shared owner exceeds the context ceiling, its sibling checks are kept together long enough for bounded recovery to prepare smaller evidence. Checks that arrive at identical compacted evidence are repacked before transport. Independent ready batches within a large file may run concurrently, while one client semaphore and one request limiter enforce scan-wide concurrency and pacing.

The built-in rules also use narrower primary context where the question permits it. JEV04, JEV05, JEV06, and JEV08 start from owner context rather than a complete file. Targeted enrichment then supplies only rule-declared relationship families when missing evidence warrants it. JEV07 and JEV09 already judge a complete file and do not perform cross-source enrichment by default.

The packaged additions are:

```yaml
scan:
  max_full_file_lines: 3000
enrichment:
  mode: targeted
budget:
  max_requests: 1500
  max_input_tokens: 5000000
  max_cost: null
  input_cost_per_million: 0.042
```

These settings can be overridden without changing schema version 4.
