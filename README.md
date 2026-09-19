# jevscan

jevscan scans Python, Rust, Perl, TypeScript, and JavaScript source. Tree-sitter establishes syntax facts and source
locations; Jev makes the configured semantic judgments. Source is sent to TypeSafe only during a live scan. Offline
scans parse files and list discovered units without API calls or cache writes.

## Quick start

From a source checkout, install Python 3.12 or newer and `uv`:

```bash
uv sync --locked

# Inventory only: no API calls or cache writes.
uv run jevscan src --offline

# Live analysis sends selected source and context to TypeSafe.
export TYPESAFE_API_KEY='your-key'
uv run jevscan src                   # confirmed and uncertain warning/error signals
uv run jevscan src -v                # every evaluated answer
uv run jevscan src --format jsonl -o report.jsonl

# No API key or paid inference: estimate initial packing and input cost.
uv run jevscan src --plan

# Hard live guards; unfinished checks remain explicit incomplete coverage.
uv run jevscan src --max-requests 500 --max-cost 0.20
uv run jevscan src --enrichment-mode off
```

To install a built wheel instead of running from the checkout:

```bash
uv tool install ./dist/py3_jevscan-0.2.0rc7-py3-none-any.whl
```

`TYPESAFE_API_KEY` is read from the environment. It is not accepted in YAML or command-line arguments. Live scans send
selected source and evidence to the configured TypeSafe endpoint. Review that data-sharing and billing boundary before
scanning private or large repositories.

Useful commands:

```bash
uv run jevscan --init-config
uv run jevscan --show-config
uv run jevscan --list-rules
uv run jevscan src --select JEV --ignore JEV09
uv run jevscan src --rule JEV04 --no-enrichment
uv run jevscan src --format json -o report.json
```

`--init-config` refuses to overwrite an existing file. `--show-config` prints the complete resolved YAML. `--list-rules`
shows names, titles, rulesets, and effective enablement. `--rule` is an alias for `--select`; both can be repeated.

## Results and exit status

Text output shows confirmed warnings and errors by default. Use `-v` to show every answer, including clean and
uncertain results. JSON and JSONL preserve raw typed answers, target attribution, evidence coverage, skipped-rule
reasons, enrichment audits, and inference metrics. Each rule entry records its own question bytes and nests the shared
request's serialized input/state/question bytes and provider `usage.input_tokens`/`usage.output_tokens`. Shared request
totals must not be summed once per rule; use `request_sha256` to deduplicate them. Provider-reported usage is never
invented; `--plan` and live budget reservations expose separately labeled conservative estimates.

Exit codes are:

| Code | Meaning |
| --- | --- |
| `0` | No confirmed finding at or above `--fail-on`, or `--fail-on never` |
| `1` | A confirmed finding meets `--fail-on` |
| `2` | The scan is incomplete or an operational/configuration error occurred |

Reduced evidence, omitted checks, and isolated provider request failures are visible as incomplete coverage. A
deterministic applicability exclusion counts as not applicable, not as a skipped or incomplete model check; its declared
missing syntax fact and rule are recorded separately in machine output.

## Configuration

Configuration is YAML in `jevscan.yaml` or `jevscan.yml`. Version 4 is required. Project configuration is additive:
the packaged JEV01–JEV09 rules remain active unless selected or ignored explicitly. Rules merge by `name`; mappings
merge recursively and lists replace. There is no `extends` setting.

```yaml
version: 4
lint:
  select: [ALL]
  ignore: []
rulesets:
  project:
    enabled: true
    description: Project-specific checks
rules:
  - name: TEAM01
    title: blocking-io-in-async-operation
    ruleset: project
    target: unit
    context: owner
    applies_to: [function, method]
    languages: [python]
    require_body: true
    question:
      type: noul
      instructions: Does the target perform clearly blocking I/O on the event-loop thread?
      criteria:
        "true": The target performs blocking I/O without an explicit offload or nonblocking boundary.
        "false": The target is nonblocking, explicitly offloaded, or no blocking I/O is visible.
    report:
      message: Review blocking I/O in this async operation.
      uncertain_range: [0.4, 0.6]
      levels:
        warning:
          min_probability: 0.75
        error:
          min_probability: 0.95
```

### Rule fields

Every rule needs a unique `name`, a `question`, and a `report`. `title` is the display label. `ruleset` groups rules
and defaults to `project`. `enabled` disables a rule without deleting its definition.

`target` is `unit` or `file`. Unit rules require `applies_to`; file rules use `context: file` and no
`applies_to`, `require_body`, or `require_members`. Supported `context` values are `unit`, `owner`, and `file`.
`owner` means the lexical owner of a callable; Rust owner context starts with the file. `languages` defaults to all
five supported languages. `require_body` excludes declarations and obvious stubs. `require_members` requires
implemented callable members for an owner-level target.

`enrich` defaults to true. `enrich_on` is a replaceable list containing `missing_evidence`, `reduced_context`,
`applicability`, `low_confidence`, `low_choice_probability`, `weak_defect_signal`, or `probability_ambiguous`.
Enrichment is bounded and retrieves local source candidates only; it is not a resolved call graph.

### Noul

A Noul asks one yes/no proposition and returns the probability that the proposition is true. Built-in Noul questions
use native TypeSafe `criteria.true` and `criteria.false` descriptions. Criteria are optional for compatibility with
simple custom rules, but explicit descriptions are recommended.

```yaml
rules:
  - name: TEAM02
    title: public-api-without-contract
    target: unit
    applies_to: [function, method]
    require_body: true
    question:
      type: noul
      instructions: Does the target expose a public operation without a visible input contract?
      criteria:
        "true": The public operation lacks a visible validation or documented input contract.
        "false": A contract is visible, the operation is not public, or the evidence does not show a missing contract.
    report:
      message: This public operation may lack an explicit input contract.
      expected: true
      uncertain_range: [0.4, 0.6]
      levels:
        warning:
          min_probability: 0.70
        error:
          min_probability: 0.92
```

`report.expected: false` reports low Noul values instead of high values. `uncertain_range` applies to the raw Noul
value and must contain `0.5`. Noul levels use `min_probability`; Noul levels do not use confidence thresholds.

### Choice

A Choice selects exactly one label from `question.criteria`. `report.choices` identifies defect labels,
`uncertain_choices` identifies missing-evidence labels, and `not_applicable_choices` identifies clean exclusions. These
sets must be disjoint.

```yaml
rules:
  - name: TEAM03
    title: concealed-failure
    target: unit
    applies_to: [function, method]
    context: file
    require_body: true
    question:
      type: choice
      instructions: Select the single outcome for fallback behavior visible in the target.
      criteria:
        concealed: A visible fallback silently hides an established invariant failure.
        legitimate: No concealment is visible or the fallback handles a legitimate runtime condition.
        missing: The fallback is visible but its invariant or failure contract is absent from the evidence.
    report:
      message: A fallback may conceal an invariant failure.
      choices: [concealed]
      uncertain_choices: [missing]
      levels:
        warning:
          min_probability: 0.70
          min_confidence: 0.55
        error:
          min_probability: 0.92
          min_confidence: 0.75
```

Choice levels require `min_probability` and may set `min_confidence`. `report.choices` is required for Choice rules.

### Score

A Score selects one zero-based level from an ordered, one-dimensional `question.criteria` list. Use concrete levels
that describe the same dimension. Score levels use exactly one of `min_score` or `max_score`, with the same direction
for warning and error.

```yaml
rules:
  - name: TEAM04
    title: control-flow-traceability
    target: unit
    applies_to: [function, method]
    require_body: true
    question:
      type: score
      instructions: Select the level for how many important execution transitions are obscured.
      criteria:
        - "0: The main path and every important transition are direct."
        - "1: One local complication exists, but transitions remain easy to trace."
        - "2: Multiple branches obscure at least one important transition."
        - "3: The main path cannot be reconstructed directly."
    report:
      message: The main execution path is difficult to trace.
      levels:
        warning:
          min_score: 1
        error:
          min_score: 2
```

### Declarative applicability

Tree-sitter extracts syntax-neutral facts per target. A rule may declare prerequisites:

```yaml
rules:
  - name: TEAM05
    title: fallback-contract
    target: unit
    applies_to: [function, method]
    require_body: true
    applicability:
      requires_any: [fallback_candidate]
    question:
      type: noul
      instructions: Does the target conceal an invariant failure behind a fallback?
      criteria:
        "true": A fallback conceals an established invariant failure.
        "false": No such concealment is visible.
    report:
      message: Review this fallback.
      levels:
        warning: {min_probability: 0.75}
        error: {min_probability: 0.95}
```

The supported syntax facts are `validation_candidate`, `fallback_candidate`, `helper_relationship`, and
`executable_behavior`. `requires_any` needs at least one listed fact; `requires_all` needs every listed fact. The two
lists cannot overlap. An absent `applicability` policy means no deterministic gate. Built-in and custom rules use this
same path. Validation and fallback facts deliberately admit broad call, predicate, and control-flow syntax; Jev still
decides whether that syntax has the rule's semantic meaning. `helper_relationship` requires a visible lexical call
between implemented siblings. An exclusion records the declared missing fact and does not mark the scan incomplete.

### Declarative targeted enrichment

Rules that have a concrete missing answer and known useful evidence families can opt into direct code-owned retrieval:

```yaml
rules:
  - name: TEAM06
    title: contract-needs-caller-evidence
    target: unit
    context: file
    applies_to: [function, method]
    require_body: true
    enrichment_families: [callers, callees, tests]
    targeted_enrichment:
      when_choices: [missing]
      when_reasons: [missing_evidence]
    question:
      type: choice
      instructions: Select the outcome for the target's contract.
      criteria:
        defect: The target violates its contract.
        clean: The target is justified or no violation is visible.
        missing: A concrete contract check is visible but required evidence is absent.
    report:
      message: Review the target contract.
      choices: [defect]
      uncertain_choices: [missing]
      levels:
        warning: {min_probability: 0.70}
        error: {min_probability: 0.92}
```

`enrichment_families` accepts `callers`, `callees`, `tests`, and `enclosing_context`. `callees` means syntax-linked
referenced definitions. `targeted_enrichment` is optional; `when_choices` refers to Choice labels and `when_reasons`
refers to the `enrich_on` reason vocabulary.
Without it, custom rules retain the generic disposition-and-family routing path. Declared targeted enrichment retrieves
candidates in code, batches independent relevance questions over shared candidate state where budgets allow, and
reassesses once only when useful evidence is admitted.

## Built-in rules

| Name | Title | Target |
| --- | --- | --- |
| JEV01 | mixed-responsibilities | unit |
| JEV02 | unclear-control-flow | unit |
| JEV03 | mixed-abstraction-levels | unit |
| JEV04 | redundant-validation | unit |
| JEV05 | hidden-invariant-failure | unit |
| JEV06 | unhelpful-decomposition | unit |
| JEV07 | fragmented-ownership | file |
| JEV08 | incohesive-owner | unit |
| JEV09 | duplicated-behavior | file |

Inspect the fully resolved catalogue with:

```bash
uv run jevscan --show-config
uv run jevscan --list-rules
```

## Further documentation

- [Configuration reference](docs/CONFIGURATION.md)
- [Jev request and response contract](docs/JEV_API.md)
- [Context recovery](docs/CONTEXT_RECOVERY.md)
- [Evidence enrichment](docs/ENRICHMENT.md)
- [Verification and live acceptance boundaries](docs/VERIFICATION.md)

## Targets and evidence are different

A **target** is what a judgment describes: a complete lexical unit or a complete file. **Context** is the surrounding evidence that judgment may use.

A method can be judged independently while Jev sees its whole class. Checks sharing context are packed into requests: source appears once, and each question identifies its target by path, qualified name, UTF-8 byte span, and line span. Local question bindings attribute answers; no generated text is interpreted as an identity.

`unit` context means the target itself. `owner` means the enclosing class, impl, or callable for methods/nested functions; a top-level function uses its file. Owner-level checks see that owner. Rust owner context starts with the file to include sibling type declarations and impls, without claiming compiler-level resolution. File checks inspect the whole file, including top-level code; they are not averages of unit scores. Module/tree targets are unsupported.

Jev-1.13 planning uses two published provider ceilings: **32,000 tokens for state plus the longest question** and **64,000 tokens for state plus all questions**. The packaged configuration keeps conservative 28,000/56,000 thresholds so the byte-based estimator has headroom. Setting either local threshold to `null` removes only that extra margin; known model ceilings still apply. Explicit values above a known model ceiling are clamped to the provider ceiling. Unknown model IDs fall back to the configured limits and the byte/question caps.

Preflight distinguishes the two dimensions before making an HTTP request. Aggregate/question-count overflow with fitting evidence is handled by splitting questions while retaining the exact state. If a shared owner is itself over the context ceiling, sibling checks stay together until recovery prepares smaller exact evidence; checks that converge on identical evidence are repacked into mixed Noul/Choice/Score batches. The run also observes successful `usage.input_tokens` and can only tighten the byte/token estimate; it never relaxes the configured or provider limits.

Batching follows **evidence identity**, not rule identity. Different rules and different targets share one System One request whenever they refer to the same exact state and fit the provider budgets. The planner does not enlarge a small owner merely to manufacture sharing: input-token reuse, not minimum HTTP count by itself, is the objective.

Complete-file semantic checks have a separate `scan.max_full_file_lines` ceiling (3,000 by default). Exceeding it produces an explicit `file-size-limit` coverage error and omits only checks that truly require the complete file; unit/owner checks continue. The limit is an operational policy, not a semantic claim that a 3,001-line file is inherently defective.

`--plan` parses and packs without contacting TypeSafe. It reports a conservative initial request/input estimate before compaction, enrichment, retries, or warm judgment-cache reuse. Live guards can cap request attempts, estimated input tokens, or configured input cost. The packaged profile stops after 1,500 request attempts or 5,000,000 estimated input tokens unless those guards are overridden in YAML; budget exhaustion leaves remaining work incomplete.

Provider rejection remains a second line of defence because the local estimator is not TypeSafe's tokenizer. HTTP 413 and exact structured `max_tokens_exceeded` machine values trigger bounded size recovery. A rejected state becomes a per-file scheduling hint so hundreds of sibling checks do not repeatedly send it. Identical failed request bytes are not replayed. Unknown request-local HTTP 400/422 responses are sanitized, attributed to the affected checks and allowed to continue; three equivalent failures trip a scan-wide circuit breaker. Authentication/permission and other systemic failures still abort.

For units, recovery constructs exact AST source spans: the complete target, affordable lexical headers, referenced local definitions/types, fields, constructors, and imports. If the candidate source does not fit, optional Jev questions rank the remaining blocks. Those questions are built from **the active YAML rule's full instructions and criteria**, for custom and built-in rules alike. No rule ID such as JEV04 is hard-coded into relevance logic. Ranking is evidence selection, not proof that omitted code is irrelevant.

The default recovery policy allows three progressively smaller compacted requests (24k estimated tokens initially, halved on further attempts; serialized bytes decrease too), then at most one final complete-target attempt. All selection calls share a bounded per-file budget. A file target is never pruned: if its complete source/question cannot be evaluated, it is explicitly omitted rather than scored from fragments. `evaluation.oversized_context: skip` forbids context reduction. Reduced requested evidence and omissions remain incomplete coverage, including exit status 2. See [recovery contracts and research](docs/CONTEXT_RECOVERY.md).

## Bounded evidence enrichment

Only actionable uncertainty is reviewed by default: missing evidence, reduced context, and eligible applicability questions. Scheduling prioritizes evidence gaps across the whole file before consuming review slots. Ordinary low confidence or ambiguous probabilities do not automatically trigger retrieval. Rules can customize `enrich_on`.

In `targeted` mode, each rule declares which evidence families may matter. `callers`, `callees` (syntax-linked referenced definitions), `tests`, and `enclosing_context` remain independent possibilities; `full` exposes all families and `off` performs no cross-source enrichment. The disposition Choice and selected family Nouls share one bounded request when they fit. Several families or none can qualify; their probabilities are not competing shares of one distribution.

A confident `local_evidence` disposition allows qualifying families through. `sufficient`, `not_applicable`, `unavailable`, uncertain disposition, or no qualifying family stops explicitly. Family results do not override a stop disposition. Small request budgets split the routing questions, rather than ignoring those budgets.

Candidates from all qualifying families are deduplicated and combined in deterministic round-robin order under **one global candidate limit**. Jev judges candidate relevance independently, then complete fitting source is added. The original question is reassessed once. Its state contains source and provenance, **not the previous verdict or routing/relevance scores**. Still uncertain means still uncertain; there is no retry loop to manufacture confidence.

```bash
uv run jevscan src --no-enrichment
uv run jevscan src --enrichment-mode targeted  # default: only rule-declared families
uv run jevscan src --enrichment-mode full      # broader, potentially more expensive
```

Global `enrichment.enabled: false`, per-rule `enrich: false`, or `enrich_on: []` also disables refinement. Defaults limit reviews to 12 checks and 36 auxiliary prediction requests per file, 12 candidates total per review, 3 admitted evidence units, and a lazy project catalogue of at most 1,000 source files / 16 MiB. Cache hits count toward logical limits. Existing token/byte/question budgets apply in every phase.

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

Callback names use syntax-derived labels such as `describe["mutation renderer"].test["mounts ..."]`, `Promise[arg1]`, `queueMicrotask[arg1]`, and `OutputWaitOwner.pump.then[arg2]`. Generic argument roles do not claim a particular runtime API. Canonical byte-span IDs, lexical names, retrieval references, and the set of scanned callbacks are unchanged; labels are presentation metadata.

Coverage warnings no longer flood the terminal. While live requests are active, an interactive terminal shows one updating progress line with sent/completed requests, cache hits, input tokens, and elapsed time. A single per-file summary reports compacted targets and omitted checks. Detailed `context-reduced` / `evaluation-size-limit` diagnostics stay in JSON/JSONL; even `-v` does not print hundreds of them. Genuine parser, filesystem and API errors remain visible immediately. The new `context_selection` audit is separate from post-judgment enrichment `reviews`.

All text, including messages and long titles, wraps to terminal display width with continuation indentation. Colors are automatic for terminals; `COLOR=yes|no` overrides detection and `NO_COLOR` disables them. Display limits count targets after filtering. Diagnostics and summaries are never hidden. Offline inventory lists units without `-v`.

JSON/JSONL **schema 8** includes all raw answers, stable rule IDs, `rule_metadata` (title/ruleset), statuses, separate confirmed/tentative findings, evidence/model/cache provenance, review audits, context-selection traces, and sanitized provider-request-rejection counters/metadata. Verbosity and display limits do not filter machine reports. Review audits preserve initial answers, disposition/family predictions, candidate family membership, selected spans, omissions, and stopping outcomes. JSONL flushes each event. File results are grouped when evaluation finishes; files may finish in any order.

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

Files and independent ready request batches inside a large file can run concurrently. One client semaphore and one rate
limiter bound concurrency and pacing across the whole scan. Full source/context is retained for active files; the
optional shared source catalogue has separate limits.

Live requests use `POST https://api.typesafe.ai/v1/systemone`. Authentication comes only from `TYPESAFE_API_KEY`. Model precedence is CLI, then `TYPESAFE_DEFAULT_MODEL`, then YAML. `TYPESAFE_BASE_URL` is an environment-only origin override; project config cannot redirect credentials. HTTPS is required except for loopback tests, and redirects are disabled.

The SQLite cache stores raw responses and independently reusable validated judgments, not submitted source or credentials. Request-cache identity still follows the canonical request. Judgment-cache identity follows endpoint, model, exact evidence, exact bound question, and prompt compatibility, so changing batch composition or an unrelated selected rule does not repurchase an unchanged judgment. Changing required context or the question still invalidates it. Pin the model for reproducibility; an alias may reuse cached results until expiry. `--no-cache` disables reads and writes. `--no-enrichment` disables cross-file uncertainty refinement, not local size recovery; set `compaction.semantic: false` to disable auxiliary Jev ordering during recovery. Reports contain source metadata and are potentially sensitive.

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
The repository uses `uv run pytest`, `uv run ruff format`, `uv run ruff check`, `uv run ty check`, and
`uv run radon cc -s src/jevscan`. No human-labeled corpus is bundled, so numerical thresholds and candidate retrieval
are not claims of calibrated semantic accuracy.
