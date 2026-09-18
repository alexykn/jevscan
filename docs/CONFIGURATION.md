# Configuration

## Discovery and precedence

Use `jevscan.toml` with `version = 4` (version 4 is also the default). Python's `tomllib` parses TOML; Tomli-W writes `--show-config`. Input is limited to 1 MiB. Invalid/duplicate TOML keys, duplicate rule names, unknown settings/selectors, and invalid rule contracts fail before scanning.

Resolution is explicit `--config`; otherwise the nearest config above the working directory; then the targets' nearest config; finally the packaged defaults. Upward discovery stops at a Git boundary. One config applies to the invocation; targets with different configs require separate scans or an explicit config. A selected config's directory is the project root. Without one, the nearest `.git`, `pyproject.toml`, `Cargo.toml`, or `package.json` determines the root.

Legacy YAML filenames are detected. A legacy-only selection raises a migration error; multiple old/new configs in one directory are ambiguous. Explicit `--config` must point to TOML. Source API-key/origin configuration remains environment-only.

`--init-config` writes a minimal additive file without overwriting. `--show-config` writes all resolved definitions and selection policy as TOML that can be read back. The latter is a snapshot of current defaults; a small override is usually easier to maintain across upgrades.

## Additive rule catalogue

The packaged rules are **always loaded**. Project settings merge into them: mappings recursively merge, lists replace, and the `[[rules]]` array is merged by `name`, not by array position. `rules = []` adds nothing; it does not delete defaults. There is no `extends` field.

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

The `JEV` ruleset contains built-ins. An initially empty `project` ruleset is available for custom rules that omit `ruleset`. Declare other sets using `[rulesets.NAME]`; `description` is optional and `enabled` defaults to true.

```toml
version = 4

[lint]
ignore = ["JEV09"]

[rulesets.TEAM]
description = "Team conventions"

[[rules]]
name = "TEAM01"
title = "mixed-responsibilities-at-boundaries"
ruleset = "TEAM"
applies_to = ["function", "method"]
require_body = true
context = "file"
[rules.question]
type = "noul"
instructions = "Does the target mix transport handling with business policy in a way that obscures either?"
[rules.report]
message = "Review the boundary between transport and business policy."
uncertain_range = [0.4, 0.6]
[rules.report.levels.warning]
min_probability = 0.75
[rules.report.levels.error]
min_probability = 0.95

[[rules]]
name = "JEV02"
[rules.report.levels.warning]
min_score = 1.2
```

This keeps all built-ins except ignored JEV09, adds TEAM01, and overrides only JEV02's warning score. Both rule and ruleset `enabled = false` are hard disables; neither is silently undone by selection. New rules need complete question/report contracts. Existing names are intentional overrides; duplicate names within the same project document are errors. To use a different primitive or remove inherited optional fields, define a new rule and ignore the old one rather than depending on TOML nulls (TOML has no null value).

## Select and ignore

`[lint] select = ["ALL"]` is the default: all enabled built-in **and custom** rules are eligible. An explicit `select` replaces that baseline. `ignore` removes matching rules. Selectors are exact rule names, exact ruleset names, or `ALL`; there is no prefix or glob interpretation.

```toml
[lint]
select = ["TEAM", "JEV02"]
ignore = ["TEAM02"]
```

Effective selection is: enabled rule, enabled ruleset, matched by `select`, and not matched by `ignore`. Ignore wins even over an exact rule selection. `select = []` or `ignore = ["ALL"]` selects nothing; a live scan then errors instead of reporting everything clean. Offline inventory remains available.

`--select NAME` (alias `--rule`) is repeatable and replaces configured `select`. Repeatable `--ignore NAME` adds to configured ignores. Use `--list-rules` to see effective enablement, including suppressed definitions. Rules are selected before question creation: ignored rules are not sent to Jev and do not become findings.

## Rule target and evidence

`target = "unit"` (default) or `"file"` determines attribution. Unit rules require nonempty `applies_to`; file rules require file context, no `applies_to`, and no body/member requirements. `module` and `tree` are not supported.

`context` is `unit`, `owner`, or `file`. Defaults are owner for units and file for files. Owner context uses the lexical owner, or the file for top-level functions; Rust owner context starts with the file. `languages` defaults to the five supported languages. `require_body = true` excludes declarations and obvious stub implementations. `require_members = true` requires implemented callable members for owner-level checks. No heuristic claims compiler-level call resolution.

`enrich` defaults to true. `enrich_on` is a replaceable list of triggers. Packaged defaults admit missing/reduced evidence; JEV06 also admits applicability. Available values are `missing_evidence`, `reduced_context`, `applicability`, `low_confidence`, `low_choice_probability`, `weak_defect_signal`, and `probability_ambiguous`. The first two have priority across a file. Additional triggers should be enabled only when evidence may help that rule; ordinary ambiguity is not evidence of missing code.

## Questions and reporting

All rules need `question.type`, focused `question.instructions`, `report.message`, and both `report.levels.warning` and `.error`. Instructions and criteria describe the target; supplied surrounding source is evidence. Rule IDs are local identities, not substitutes for complete instructions.

### Noul

Noul is a scalar probability of a proposition, not intensity. Levels use `min_probability` and no separate confidence. `report.expected = false` tests `1 - noul`. Optional `uncertain_range = [0.4, 0.6]` applies to raw Noul with an inclusive lower/exclusive upper boundary. It must enclose 0.5. A signal inside that band can still be displayed as an uncertain warning/error when it crosses the directional reporting threshold.

### Choice

Choice uses named criteria and reports only selected defect labels listed in `report.choices`. Both levels require `min_probability`; `min_confidence` is optional. `uncertain_choices` identifies missing-evidence answers; `not_applicable_choices` is separate. These categories must be disjoint and present in `question.criteria`.

```toml
[[rules]]
name = "PROJECT02"
title = "concealed-failure"
applies_to = ["function", "method"]
context = "file"
require_body = true
[rules.question]
type = "choice"
instructions = "Does this operation silently conceal a violation of an explicitly established internal invariant?"
[rules.question.criteria]
concealed = "An established internal invariant is violated and silently concealed."
legitimate = "Only legitimate runtime conditions are handled, or no concealment is present."
missing = "A necessary invariant or behavior is absent from the evidence."
[rules.report]
message = "Review the concealed invariant failure."
choices = ["concealed"]
uncertain_choices = ["missing"]
[rules.report.levels.warning]
min_probability = 0.60
min_confidence = 0.50
[rules.report.levels.error]
min_probability = 0.92
min_confidence = 0.70
```

### Score

Score uses an ordered array of criterion descriptions and a zero-based numeric scale. Levels use exactly one of `min_score` or `max_score`, with the same direction at both levels, and optional `min_confidence`. Levels must lie inside the rubric. Score reports cannot use `min_probability`, Choice labels, or `expected = false`.

### Confirmation and tentative severity

The highest matching numeric signal gives severity; confidence determines confirmation at **that** level. An error-level result lacking error-level confidence is an uncertain error, not a confirmed warning. Confident outcomes create `findings`; uncertain above-threshold outcomes create `tentative_findings`. The latter remain unknown, appear by default with cyan `?`, and do not trigger `--fail-on`.

Benign Choice labels and missing-evidence labels never acquire an invented defect severity. Ordinary unknown results below reporting thresholds require `-v`. Per-rule messages and all numerical thresholds are customizable. Thresholds remain heuristic and are not measured correctness probabilities.

## Operational settings

| Section | Important defaults / meaning |
|---|---|
| scan | Existing five-language include patterns and dependency/build excludes; respect_gitignore=true; jobs=0 (up to 8 parsers); batch_size=8; queue_size=8; max_file_bytes=2000000; max_units_per_file=10000 |
| jev | model="jev-latest"; concurrency=16; requests_per_minute=600; timeout_seconds=30; retries=3; max_retry_delay=60 |
| evaluation | max_context_tokens=28000; max_total_tokens=56000; token_reserve=512; bytes_per_token=3.0; max_request_bytes=1048576; max_questions=64; oversized_context="reduce" |
| enrichment | enabled=true; max_checks_per_file=12; max_calls_per_file=36; max_candidates=12; max_evidence=3; max_source_files=1000; max_source_bytes=16777216 |
| cache | enabled=true; path=".jevscan-cache/results.sqlite3"; ttl_seconds=86400 |

Use `--show-config` for the complete resolved values, including source patterns. API pacing is a local budget, not an account quota claim. Context estimates are not a provider tokenizer. `oversized_context = "skip"` forbids reducing requested evidence. Targets are never truncated.

Routing uses `min_route_probability=0.70` and `min_route_confidence=0.50` for the **disposition Choice**, `min_evidence_probability=0.60` independently for each evidence-family Noul, and `min_relevance=0.65` for each candidate. Do not add or normalize independent family probabilities. A disposition stop wins over speculative family answers. One review can use several families, but the candidate/evidence/call budgets stay shared, not multiplied by family count.

The cache path must be relative and inside the project. Cache entries contain raw provider answers, not source. `--no-cache` disables all cache use, including enrichment. CLI model/config operational overrides are validated by the same schema.

## Migration from YAML v3

1. Replace `jevscan.yaml`/`.yml` with `jevscan.toml`, set `version = 4`, and remove `extends`. Keep a backup outside those recognized filenames while migrating.
2. Convert the rule mapping to named `[[rules]]` entries. Use the ID table above to replace built-in slugs, including CLI `--rule` arguments and consumers of `Finding.rule`.
3. Convert YAML mappings to TOML tables and use `=` for values. Threshold names and meanings do not change.
4. Built-ins now remain active automatically. For an old standalone custom-only config, declare your custom rules and set `[lint] select = ["your-set"]` (or enumerate rule names). To disable all checks, use `select = []`, not an empty rule array.
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

```toml
version = 4
[lint]
ignore = ["JEV09"]

[[rules]]
name = "JEV02"
[rules.report.levels.warning]
min_score = 1.2
```

There is no automatic YAML rewrite or hidden legacy evaluator. Migration errors are preferable to silently changing which checks a project runs. Numerical defaults were not retuned in rc4.
