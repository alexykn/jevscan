# jevscan

A configuration-driven semantic code-quality scanner for **Python, Rust, Perl, TypeScript, and JavaScript**. Tree-sitter extracts code; Jev answers independent typed questions about it. jevscan does not execute or import the source being scanned.

**0.2.0rc3** makes evidence enrichment selective and priority-ordered, and displays tentative warning/error signals even without `-v`. It retains the bounded file/unit evaluation and one-pass evidence retrieval from RC2. This is a release candidate, not a claim of calibrated semantic accuracy. See [verification](docs/VERIFICATION.md) for tested behavior and remaining limits.

## Install and run

Python 3.12 or newer is required. The distribution is `py3-jevscan`; the command and import package are `jevscan`.

```bash
uv sync --locked

# Inventory only: no API calls or cache writes.
uv run jevscan src --offline

# Live analysis sends selected source and context to TypeSafe.
export TYPESAFE_API_KEY='your-key'
uv run jevscan src                  # warnings and errors, plus coverage diagnostics
uv run jevscan src -v               # every evaluated answer and evidence-review details
uv run jevscan src --no-enrichment  # primary judgments only; no extra source discovery
uv run jevscan src --format jsonl -o report.jsonl
```

An installed wheel works without this checkout:

```bash
uv tool install ./dist/py3_jevscan-0.2.0rc3-py3-none-any.whl
```

The pinned `tree-sitter==0.25.2` and `tree-sitter-language-pack==0.13.0` bundle the native grammars. A scan never downloads grammars. Updating those pins requires rerunning parser integration tests.

## Targets and evidence are different

A **target** is what a judgment describes: a complete lexical code unit or a complete file. **Context** is the surrounding evidence that the same judgment may use.

A method can be judged independently while Jev sees its whole class. Checks sharing an evidence envelope are packed into one request: the source appears once, and each question identifies its own target with path, qualified name, UTF-8 byte range, and line range. Responses are attributed through local question bindings, not by interpreting generated text.

```yaml
version: 3
extends: default

rules:
  mixed-responsibilities:
    target: unit
    context: owner
  redundant-validation:
    target: unit
    context: file
  duplicated-behavior:
    target: file
    context: file
```

`unit` context means the target itself. `owner` means the enclosing class, impl, or callable for methods/nested functions; a top-level function uses its file. An owner-level check sees that owner. Rust owner context starts with the file so sibling struct/enum declarations and impl blocks are visible, without claiming compiler-level name resolution.

File-target rules inspect the entire file, including top-level behavior outside classes/functions. They are not averages or maxima of unit scores. `duplicated-behavior` and `fragmented-ownership` are file-targeted in the packaged defaults; the other seven rules remain unit-targeted. `module` and `tree` targets are intentionally unsupported.

### Inputs that exceed a budget

There is **no 32 KB code-unit cutoff**. The planner checks estimated state-plus-longest-question tokens, estimated total request tokens, actual serialized bytes, and question count. Defaults are 28,000/56,000 estimated tokens, a 512-token reserve, a 1 MiB request ceiling, and 64 questions. Estimates use UTF-8 bytes, not TypeSafe's tokenizer; these are configurable application budgets, not guaranteed provider limits.

The planner splits questions while preserving their shared evidence. When a requested file/owner context cannot fit, `evaluation.oversized_context: reduce` permits narrower context while retaining the **entire target**. The report identifies the exact included/omitted source ranges and marks reduced coverage incomplete. `skip` instead requires the requested context unchanged. Explicit provider size rejection also triggers bounded question splitting/context reduction; unrelated API errors do not.

A target that cannot fit even by itself is explicitly omitted. Smaller nested units can still be evaluated. jevscan never clips a target, averages partial judgments into a whole-file score, or reports unseen code as clean. Use `--format jsonl` to inspect per-rule evidence and omissions.

## Reports

```text
src/service.py
    M 42 Coordinator.commit
        ! mixed-responsibilities  noul=0.670
          This operation appears to interleave responsibilities that would be
          clearer as distinct operations.
    FILE 1-230 whole file
        x duplicated-behavior     noul=0.960
          The same owned behavior appears implemented more than once and may
          need to be maintained in sync.
```

The default text report shows confirmed **yellow `!` warnings and red `x` errors**, plus **cyan `?` tentative warnings/errors** when a signal crosses a configured severity threshold but lacks confidence. Tentative rows carry `[uncertain warning]` or `[uncertain error]`, the configured message, and the uncertainty reason. A stronger error signal is not downgraded to warning just because confidence passes only the lower gate. `-v` / `--verbose` adds green OK results, below-threshold uncertainty, and not-applicable results. Score rows include their rubric maximum, for example `score=1.660/3`; confidence is not severity.

```text
    M 194 Enricher._select
        ? unclear-control-flow  score=1.660/3  conf=0.580  [uncertain warning]
          Interleaved concerns or nesting appear to obscure the main execution path.
          Uncertain: low confidence
```

For Noul, an answer inside `uncertain_range` remains uncertain. It is shown by default only if it also reaches a configured directional warning/error probability threshold; `0.48` remains verbose-only with the default positive rule, while `0.51` becomes a tentative warning, not a confirmed finding. A selected `insufficient_context` or `not_applicable` Choice is never labelled a defect merely because its probability is high.

The summary separates confirmed counts from tentative counts. Tentative findings remain included in `uncertain` and **do not trigger `--fail-on`**. JSON/JSONL likewise keep `findings` and `tentative_findings` separate.

All text, including explanatory messages and long names, wraps to terminal width with continuation indentation. ANSI color is automatic for terminals; `COLOR=yes|no` overrides detection and `NO_COLOR` disables it. Text files are plain unless color is explicitly forced. Display limits count targets **after** severity filtering; `--max-display 0` means unlimited. Diagnostics and the summary are never hidden. Offline inventory lists units even without `-v`.

JSON and JSONL use **report schema 4** and always contain all completed answers, target metadata, statuses, findings, per-rule evidence/model/cache provenance, explicit skipped rules, and a final summary. The additive `reviews` and `uncertainty_reasons` maps preserve initial judgments, routing/selection results, source hashes/ranges, omissions, and final outcomes; the new `not_applicable` status is distinct from OK. They are unaffected by verbosity, color, or display limits. A file's results are grouped when its evaluation finishes; files can finish in any order. JSONL flushes each event and preserves already-written events during an interrupted scan.

Exit codes:

| Code | Meaning |
| --- | --- |
| `0` | Complete scan, no confirmed findings at the selected severity; or complete offline inventory |
| `1` | Confirmed findings meet `--fail-on` (`warning` by default) |
| `2` | Invalid configuration, operational failure, omitted targets, or reduced requested context |
| `130` | Interrupted with Ctrl-C |

`--fail-on never` does not hide incomplete coverage. Model uncertainty alone does not mean an operational failure; it is counted separately. No-rule units are skipped by configuration, not counted as failed scans.

## Evidence enrichment

Only actionable uncertainty requests a review by default: **missing evidence first, reduced context second, applicability third**. Scheduling considers all pending judgments in a file before consuming review/call budgets; result display order remains unchanged. Reduced context is actionable even when low confidence is also present. Ordinary low-confidence scores and ambiguous Nouls stay uncertain without another API prediction. The applicability route is eligible only for rules declaring `not_applicable_choices`.

Each rule can override `enrich_on`; the default is `[missing_evidence, reduced_context, applicability]`. For a demonstrably context-sensitive custom rule, opt into `low_confidence` or `low_choice_probability`; `weak_defect_signal` and `probability_ambiguous` are also explicit opt-ins. Such reviews follow evidence/applicability reviews. `enrich_on: []` requests no reviews. Existing `enrich: false` and `--no-enrichment` still take precedence. None of these settings changes severity/confidence thresholds.

An uncertain answer does not automatically mean that callers are missing. The default workflow is:

```text
primary judgment
  → actionable uncertainty only: closed evidence-routing Choice
  → missing source only: bounded local candidate discovery
  → independent relevance Noul per candidate (batched)
  → selected complete source + original evidence
  → original question once more, then stop
```

Jev selects among fixed routes and candidate identities. It cannot invent a path, execute a command, or recursively fetch more source. Confident primary answers are not revisited. A routing result of “sufficient evidence” leaves the original uncertainty intact; it does not manufacture an OK result. A confident not-applicable result is displayed separately. Candidate selection asks for evidence that can decide the question, not evidence supporting the earlier verdict. The final question is unchanged and receives no prior answer or relevance scores.

**Source-sharing scope:** a live enrichment pass may read and send allowed source anywhere under the resolved project root, even when the command targets only `src/` or one file. This includes matching tests. It uses the same include/exclude and Git-ignore rules and rejects symlinked evidence paths. Only candidate previews and selected complete source ranges are sent; the whole index is not uploaded. Use `--no-enrichment`, YAML `enrichment.enabled: false`, or per-rule `enrich: false` to disable it. Offline inventory never builds the index or calls Jev.

Defaults allow one reassessment per check, at most 12 reviewed checks and 36 enrichment predictions per active file, 12 candidates per review, and 3 selected evidence units. The shared source index reads at most 1,000 files and 16 MiB (plus a one-byte overflow sentinel). Every phase also respects the ordinary request budgets. Limits and retrieval omissions remain visible in machine reports; they never imply that missing code is clean. These are adjustable application policies, not provider quotas.

Two sources of avoidable uncertainty are handled before retrieval. `require_body` excludes obvious declaration/pass/ellipsis-only callables, while the decomposition rule uses `require_members` to avoid querying data-only owners. Default Noul rules treat `[0.40, 0.60)` as undecided **before** applying severity levels; a near-0.5 answer is not automatically yellow. Existing project YAML remains valid, but projects that copied older defaults must opt into these policy fields or regenerate their configuration.

See [architecture, evidence contracts, and research](docs/ENRICHMENT.md) and [configuration](docs/CONFIGURATION.md). Lexical candidates are not a resolved call graph: aliases, dynamic dispatch, macros, and external contracts remain limitations. No live semantic-accuracy improvement is claimed from mock tests.

## Configuration

```bash
uv run jevscan --init-config       # create v3 YAML; never overwrite an existing file
uv run jevscan --show-config       # resolved settings
uv run jevscan --list-rules        # targets, context, and warning/error levels
uv run jevscan src --config quality.yaml
```

Resolution: explicit `--config`; otherwise the nearest `jevscan.yaml`/`jevscan.yml above the working directory; then the targets' nearest configuration; finally the packaged defaults. Upward discovery stops at a Git boundary. One configuration applies to the entire invocation. Ambiguous configurations fail before scanning.

A standalone YAML file **replaces** the packaged rules. Add `extends: default` to inherit them and override selected fields. Mappings merge recursively; lists replace. Both reporting levels and explanations are configurable:

```yaml
version: 3
extends: default
rules:
  unclear-control-flow:
    context: file
    report:
      message: Review this operation's control flow.
      levels:
        warning:
          min_score: 1.2
          min_confidence: 0.60
        error:
          min_score: 2.2
          min_confidence: 0.70
```

The most severe matching signal level wins; its own confidence gate determines whether it is confirmed or tentative. Changing only reporting thresholds/messages reuses eligible cached raw answers. Changing questions, source, evidence, or model changes the request identity.

See [the complete rule/configuration guide and migration instructions](docs/CONFIGURATION.md), [standalone example](examples/standalone.yaml), and [inherited example](examples/extends-default.yaml). Configuration versions 1 and 2 are rejected with a migration message rather than silently reinterpreted.

## Extraction and scope

| Language | Lexical units |
| --- | --- |
| Python | Functions, async functions, classes, nested classes, instance/static/class methods, decorated definitions |
| Rust | Functions, structs, enums, traits/default methods, impl blocks/methods, modules, type aliases, closures |
| TypeScript | Functions, classes, methods/constructors, assigned arrows, interfaces/signatures, types, namespaces; TSX |
| JavaScript | Functions, classes/methods, assigned arrows/function expressions, object methods; JSX |
| Perl | Subroutines, lexical packages, native classes/explicit methods, anonymous subroutines; `.pl`, `.pm`, `.t` |

Every unit retains its qualified name, lexical parent, signature, UTF-8 byte range, line range, and kind. Ordinary Perl `sub` declarations remain functions because parsing cannot establish runtime method dispatch. Runtime-generated methods, macros, dynamic metaprogramming, cross-file callers, and external contracts are not resolved. Branch-node counts are structural observations, **not** language-independent cyclomatic complexity.

Source must be UTF-8. Unsupported syntax, parse errors, file/read limits, and missing parsers produce explicit incomplete-coverage diagnostics. Symlinks are not followed. Include/exclude patterns and nested Git ignore files are respected; global Git excludes are not read. Dependencies, build artifacts, and common virtual environments are excluded by default.

## Execution and ownership

```text
discovery → spawned Tree-sitter parsers → bounded file queue
          → file evaluator: targets → evidence → request batches → target results
          → terminal / JSON / JSONL
```

Each evaluator owns one file's plan and results. Requests within that file run sequentially; files run concurrently up to `jev.concurrency`. This keeps target results together and avoids one task per method or retaining an entire repository's results. Parser processes, file batches, queue depth, read limits, and evaluation workers are bounded/configurable. Initial source envelopes and results remain in memory only for active files; enrichment also retains one lazily built, bounded project source index; memory depends on configured file/worker limits, unit nesting, and question count, not merely source byte size.

Module ownership in `src/jevscan/`:

| Module | Responsibility |
| --- | --- |
| `core/models.py` | Lexical units, analytical targets, findings, coverage diagnostics, summary |
| `core/rules.py`, `config.py` | Question/report contracts; safe YAML loading and operational settings |
| `core/discovery.py`, `languages.py`, `parser.py` | Source discovery and lexical extraction |
| `core/context.py` | Exact evidence envelopes and coverage descriptions |
| `core/planning.py` | Target/question bindings, request packing, size-recovery decisions |
| `core/protocol.py`, `client.py` | Wire contracts and response validation; HTTP, pacing, retries |
| `core/assessment.py` | One severity/applicability/uncertainty policy for raw answers |
| `core/inference.py` | Shared validated prediction/cache path for all phases |
| `core/retrieval.py` | Bounded local snapshots and lexical evidence candidates |
| `core/enrichment.py` | Closed routing, independent relevance judgments, one fresh reassessment |
| `core/evaluation.py` | File-local execution, answer attribution, result lifecycle |
| `core/cache.py`, `scanner.py` | Raw-answer persistence; bounded pipeline/resource orchestration |
| `cli/args.py`, `main.py`, `render.py`, `terminal.py` | Arguments/entry point; report formatting; safe width-aware terminal output |

## API and privacy

Live requests go to `POST https://api.typesafe.ai/v1/systemone`; source and bounded context leave the machine. Authentication is read only from `TYPESAFE_API_KEY`. Model precedence is CLI `--model`, then `TYPESAFE_DEFAULT_MODEL`, then YAML. `TYPESAFE_BASE_URL` is an environment-only origin override; repository YAML cannot redirect the API key. HTTPS is required except for loopback tests, and redirects are disabled.

The SQLite cache stores raw answers, not submitted source or credentials. Keys include endpoint, request body, package version, and prompt version. Reports include declaration metadata and an audit of evidence selection (paths, symbols, hashes, locations and raw judgments), and should also be treated as potentially sensitive. Raw candidate source previews are not copied into reports. A moving model alias can reuse cached responses until expiration; pin a model for reproducibility. `--no-cache` disables both cache reads and writes.

Retries can duplicate provider costs. Rate pacing counts all attempts, including recovery calls. The scanner is not a sandbox for hostile filesystems or a proven defense against prompt injection. [API contracts and evidence semantics](docs/JEV_API.md).

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

CI checks the full source/test tree, runs real bundled parser tests, and builds and smoke-tests the installed distribution outside the checkout. Tests mock only the HTTP boundary; no provider API key is needed. See [verification](docs/VERIFICATION.md) for the precise distinction between software checks and live semantic evaluation.
