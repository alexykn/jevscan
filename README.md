# jevscan

A configuration-driven semantic code-quality scanner for **Python, Rust, Perl, TypeScript, and JavaScript**. Tree-sitter extracts source; Jev answers independent typed questions. jevscan does not execute or import the code being scanned.

**0.2.0rc4** introduces additive `jevscan.toml` configuration, named rules and rulesets, and multi-label evidence routing. This is a release candidate, not a claim of calibrated semantic accuracy. [Verification](docs/VERIFICATION.md) separates software checks from live model acceptance.

## Install and run

Python 3.12 or newer is required. The distribution is `py3-jevscan`; the command and import package are `jevscan`.

```bash
uv sync --locked

# Inventory only: no API calls or cache writes.
uv run jevscan src --offline

# Live analysis sends selected source and context to TypeSafe.
export TYPESAFE_API_KEY='your-key'
uv run jevscan src                   # confirmed and uncertain warning/error signals
uv run jevscan src -v                # every evaluated answer
uv run jevscan src --format jsonl -o report.jsonl
```

An installed wheel works without this checkout:

```bash
uv tool install ./dist/py3_jevscan-0.2.0rc4-py3-none-any.whl
```

The pinned `tree-sitter==0.25.2` and `tree-sitter-language-pack==0.13.0` bundle native grammars. Scans never download grammars. Updating these pins requires parser integration tests.

## Named rules and additive configuration

A project `jevscan.toml` **adds to the built-in catalogue**. There is no `extends` switch and an empty project configuration does not remove the defaults. Rules merge by their unique `name`; nested settings merge, while lists replace. A new name adds a rule. An existing name overrides only the supplied fields.

```toml
version = 4

[lint]
ignore = ["JEV09"]  # Ignore one rule. Use "JEV" to ignore the built-in ruleset.

[[rules]]
name = "JEV02"
[rules.report.levels.warning]
min_score = 1.2     # Override just this threshold; the rest stays inherited.
```

Names are stable identities; titles describe the check. The built-in ruleset is `JEV`:

| Name | Title | Target |
|---|---|---|
| JEV01 | mixed-responsibilities | unit |
| JEV02 | unclear-control-flow | unit |
| JEV03 | mixed-abstraction-levels | unit |
| JEV04 | redundant-validation | unit |
| JEV05 | hidden-invariant-failure | unit |
| JEV06 | unhelpful-decomposition | unit |
| JEV07 | fragmented-ownership | file |
| JEV08 | incohesive-owner | unit |
| JEV09 | duplicated-behavior | file |

Custom rules use the same selection and reporting contracts:

```toml
[rulesets.TEAM]
description = "Team-specific conventions"

[[rules]]
name = "TEAM01"
title = "blocking-io-in-async-code"
ruleset = "TEAM"
target = "unit"
context = "file"
applies_to = ["function", "method"]
require_body = true

[rules.question]
type = "noul"
instructions = "Does this async operation perform clearly blocking I/O on the event-loop thread?"

[rules.report]
message = "Review blocking I/O in this async operation."
uncertain_range = [0.4, 0.6]
[rules.report.levels.warning]
min_probability = 0.75
[rules.report.levels.error]
min_probability = 0.95
```

This adds `TEAM01` without removing `JEV01`–`JEV09`. A rule without `ruleset` belongs to the built-in empty `project` group. Other groups must be declared. To disable a whole set, set `[rulesets.TEAM] enabled = false`; to disable a rule, override its `enabled = false`. `[lint] ignore = ["TEAM"]` also suppresses the whole set. To run only custom rules, explicitly use `[lint] select = ["TEAM"]`. Definitions remain available for inspection.

Selectors accept exact rule names, exact ruleset names, or `ALL`, not arbitrary prefixes or globs. Ignore wins; `enabled = false` on either a rule or its set also wins. Unknown selectors and duplicate rule names are errors, not silently ignored typos.

```bash
uv run jevscan --init-config          # minimal additive TOML; refuses to overwrite
uv run jevscan --show-config          # full resolved, round-trippable TOML
uv run jevscan --list-rules           # identities, titles, sets, effective enablement
uv run jevscan src --select JEV --ignore JEV09
uv run jevscan src --rule TEAM01      # --rule is an alias for --select
```

Repeat `--select`/`--ignore` for several names. CLI selection replaces the configured selection; CLI ignores add to configured ignores. Explicitly disabled rules are not re-enabled.

**Migration:** YAML versions 1–3 are retired. Legacy `jevscan.yaml`/`.yml` files are detected and produce migration errors; they are not silently bypassed. Read the [v3-to-v4 migration guide](docs/CONFIGURATION.md#migration-from-yaml-v3). Rule IDs and machine reports changed too; numerical finding thresholds did not.

## Targets and evidence are different

A **target** is what a judgment describes: a complete lexical unit or a complete file. **Context** is the surrounding evidence that judgment may use.

A method can be judged independently while Jev sees its whole class. Checks sharing context are packed into requests: source appears once, and each question identifies its target by path, qualified name, UTF-8 byte span, and line span. Local question bindings attribute answers; no generated text is interpreted as an identity.

`unit` context means the target itself. `owner` means the enclosing class, impl, or callable for methods/nested functions; a top-level function uses its file. Owner-level checks see that owner. Rust owner context starts with the file to include sibling type declarations and impls, without claiming compiler-level resolution. File checks inspect the whole file, including top-level code; they are not averages of unit scores. Module/tree targets are unsupported.

There is **no 32 KB unit cutoff**. The planner checks estimated state-plus-longest-question tokens, aggregate tokens, serialized bytes, and question count. Defaults are 28,000/56,000 estimated tokens, a 512-token reserve, 1 MiB, and 64 questions. The UTF-8 byte estimator is not TypeSafe's tokenizer or a guarantee about provider limits.

Oversized requests first split questions. `evaluation.oversized_context = "reduce"` permits narrower surrounding context but retains the entire target; `"skip"` requires the requested context unchanged. Explicit provider size rejection also triggers bounded recovery. Reduced context and omitted checks are reported as incomplete coverage. Smaller nested units can still be evaluated. Targets are never clipped or falsely reported as clean.

## Bounded evidence enrichment

Only actionable uncertainty is reviewed by default: missing evidence, reduced context, and eligible applicability questions. Scheduling prioritizes evidence gaps across the whole file before consuming review slots. Ordinary low confidence or ambiguous probabilities do not automatically trigger retrieval. Rules can customize `enrich_on`.

One routing request normally contains a **disposition Choice** and four **independent Nouls** for callers, definitions, tests, and enclosing context. The family questions use an explicit speculative premise and cannot see each other's answers. Several families or none can qualify; their probabilities are not competing shares of one distribution.

A confident `local_evidence` disposition allows qualifying families through. `sufficient`, `not_applicable`, `unavailable`, uncertain disposition, or no qualifying family stops explicitly. Family results do not override a stop disposition. Small request budgets split the routing questions, rather than ignoring those budgets.

Candidates from all qualifying families are deduplicated and combined in deterministic round-robin order under **one global candidate limit**. Jev judges candidate relevance independently, then complete fitting source is added. The original question is reassessed once. Its state contains source and provenance, **not the previous verdict or routing/relevance scores**. Still uncertain means still uncertain; there is no retry loop to manufacture confidence.

```bash
uv run jevscan src --no-enrichment
```

Global `enrichment.enabled = false`, per-rule `enrich = false`, or `enrich_on = []` also disables refinement. Defaults limit reviews to 12 checks and 36 auxiliary prediction requests per file, 12 candidates total per review, 3 admitted evidence units, and a lazy project catalogue of at most 1,000 source files / 16 MiB. Cache hits count toward logical limits. Existing token/byte/question budgets apply in every phase.

**Source-sharing boundary:** enrichment may select source outside the scanned subdirectory, but only inside the resolved project root and existing source/include/exclude/Git-ignore filters. Reads reject symlinks, including parent components, nonregular files, and observed read-time changes. Previews and selected source use the same immutable per-file snapshot. A lexical match is a *possible* caller/definition, not a resolved call graph or proof of a universal guarantee. [Design and limitations](docs/ENRICHMENT.md).

## Reports

```text
src/service.py
    M 42 Coordinator.commit
        ! JEV01 mixed-responsibilities  noul=0.670
          This operation appears to interleave responsibilities that would be
          clearer as distinct operations.
        ? JEV02 unclear-control-flow    score=1.640/3  conf=0.580  [uncertain warning]
          Uncertain: low confidence
```

Confirmed warnings are yellow `!`; errors are red `x`. Signals that cross a configured threshold but lack sufficient confidence appear as cyan `?` uncertain warnings/errors **without `-v`**. An error-level signal remains an uncertain error rather than being downgraded to a confirmed warning. Tentative findings are distinct from confirmed findings and do not trigger `--fail-on`. `-v` adds green OK answers, ordinary uncertainty below finding thresholds, and not-applicable results.

All text, including messages and long titles, wraps to terminal display width with continuation indentation. Colors are automatic for terminals; `COLOR=yes|no` overrides detection and `NO_COLOR` disables them. Display limits count targets after filtering. Diagnostics and summaries are never hidden. Offline inventory lists units without `-v`.

JSON/JSONL **schema 5** includes all raw answers, stable rule IDs, `rule_metadata` (title/ruleset), statuses, separate confirmed/tentative findings, evidence/model/cache provenance, and review audits. Verbosity and display limits do not filter machine reports. Review audits preserve initial answers, disposition/family predictions, candidate family membership, selected spans, omissions, and stopping outcomes. JSONL flushes each event. File results are grouped when evaluation finishes; files may finish in any order.

| Exit | Meaning |
|---|---|
| 0 | Complete scan without confirmed findings at `--fail-on`, or complete offline inventory |
| 1 | Confirmed findings meet `--fail-on` (warning by default) |
| 2 | Configuration/operational failure, omitted targets, or reduced requested context |
| 130 | Interrupted |

`--fail-on never` does not hide incomplete coverage. Model uncertainty is counted separately, not treated as an operational failure. Reported probabilities/confidence are provider values, not measured correctness rates.

## Extraction, execution, and privacy

The five language frontends extract lexical classes, functions/methods, supported declarations, and assigned closures. Native grammars and fixtures cover Python decorators/nesting, Rust impls/traits, TS/JS arrows and JSX/TSX, and Perl packages/classes/subroutines. Source must be UTF-8. Unsupported syntax, parse/read limits, and missing parsers produce explicit diagnostics. Ordinary Perl subs are not assumed to be runtime methods. Branch-node counts are not language-independent cyclomatic complexity.

```text
discovery → spawned Tree-sitter parsers → bounded file queue
          → file evaluator → shared-context requests → optional bounded enrichment
          → terminal / JSON / JSONL
```

Each evaluator owns one file's plan/results. Files run concurrently; requests within a file are sequential. Full source/context is retained for active files; the optional shared source catalogue has separate limits. Resource lifecycle belongs to `scanner.py`; selection/loading to `config.py`; question contracts to `rules.py`; packing to `planning.py`; evidence to `context.py`; assessment to `assessment.py`; HTTP/cache prediction to `inference.py`; local candidates to `retrieval.py`; refinement to `enrichment.py`; results to `evaluation.py`; presentation to `cli/`.

Live requests use `POST https://api.typesafe.ai/v1/systemone`. Authentication comes only from `TYPESAFE_API_KEY`. Model precedence is CLI, then `TYPESAFE_DEFAULT_MODEL`, then TOML. `TYPESAFE_BASE_URL` is an environment-only origin override; project config cannot redirect credentials. HTTPS is required except for loopback tests, and redirects are disabled.

The SQLite cache stores raw responses, not submitted source or credentials. Identity includes endpoint, request body, package version, and prompt version. Threshold/title/selection-only edits reuse eligible cached answers when the actual request is unchanged. Batch composition changes may change that identity. Pin the model for reproducibility; an alias may reuse cached results until expiry. `--no-cache` disables reads and writes. Reports contain source metadata and are potentially sensitive.

Source is not sandboxed against a hostile filesystem or proven immune to prompt injection. Aliases, dynamic dispatch, macros, external contracts, and cross-language resolution remain limited. Global Git excludes are not read. [API/evidence contracts](docs/JEV_API.md).

## Development

```bash
uv sync --locked
uv run ruff format --check src tests
uv run ruff check src tests
uv run ty check
uv run pytest -q -rs
uv run radon cc -s src
uv build
```

CI checks the full project, runs actual bundled parsers, and builds/smoke-tests installed wheels outside the checkout on Linux and macOS. Tests mock the HTTP boundary and require no provider credentials. [Configuration reference](docs/CONFIGURATION.md).
