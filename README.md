# jevscan

A configuration-driven semantic code-quality scanner for **Python, Rust, Perl, TypeScript, and JavaScript**. Tree-sitter extracts code; Jev answers independent typed questions about it. jevscan does not execute or import the source being scanned.

**0.2.0rc1** introduces file and unit targets, shared-context requests, and a warning/error-first terminal report. This is a release candidate, not a claim of calibrated semantic accuracy. See [verification](docs/VERIFICATION.md) for tested behavior and remaining limits.

## Install and run

Python 3.12 or newer is required. The distribution is `py3-jevscan`; the command and import package are `jevscan`.

```bash
uv sync --locked

# Inventory only: no API calls or cache writes.
uv run jevscan src --offline

# Live analysis sends selected source and context to TypeSafe.
export TYPESAFE_API_KEY='your-key'
uv run jevscan src                  # warnings and errors, plus coverage diagnostics
uv run jevscan src -v               # every evaluated answer
uv run jevscan src --format jsonl -o report.jsonl
```

An installed wheel works without this checkout:

```bash
uv tool install ./dist/py3_jevscan-0.2.0rc1-py3-none-any.whl
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
        ! mixed-responsibilities  noul=0.570
          This operation appears to interleave responsibilities that would be
          clearer as distinct operations.
    FILE 1-230 whole file
        x duplicated-behavior     noul=0.960
          The same owned behavior appears implemented more than once and may
          need to be maintained in sync.
```

The default text report shows **yellow `!` warnings and red `x` errors**. `-v` / `--verbose` adds green `·` OK answers and cyan `?` uncertain answers. An uncertainty answer is not a clean bill of health. Score rows include their rubric maximum, for example `score=1.140/3`; probabilities/confidence remain the provider's values, not measured correctness rates.

All text, including explanatory messages and long names, wraps to terminal width with continuation indentation. ANSI color is automatic for terminals; `COLOR=yes|no` overrides detection and `NO_COLOR` disables it. Text files are plain unless color is explicitly forced. Display limits count targets **after** severity filtering; `--max-display 0` means unlimited. Diagnostics and the summary are never hidden. Offline inventory lists units even without `-v`.

JSON and JSONL use **report schema 2** and always contain all completed answers, target metadata, statuses, findings, per-rule evidence/model/cache provenance, explicit skipped rules, and a final summary. They are unaffected by verbosity, color, or display limits. A file's results are grouped when its evaluation finishes; files can finish in any order. JSONL flushes each event and preserves already-written events during an interrupted scan.

Exit codes:

| Code | Meaning |
| --- | --- |
| `0` | Complete scan, no findings at the selected severity; or complete offline inventory |
| `1` | Findings meet `--fail-on` (`warning` by default) |
| `2` | Invalid configuration, operational failure, omitted targets, or reduced requested context |
| `130` | Interrupted with Ctrl-C |

`--fail-on never` does not hide incomplete coverage. Model uncertainty alone does not mean an operational failure; it is counted separately. No-rule units are skipped by configuration, not counted as failed scans.

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

The most severe matching level wins. Changing only reporting thresholds/messages reuses eligible cached raw answers. Changing questions, source, evidence, or model changes the request identity.

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

Each evaluator owns one file's plan and results. Requests within that file run sequentially; files run concurrently up to `jev.concurrency`. This keeps target results together and avoids one task per method or retaining an entire repository's results. Parser processes, file batches, queue depth, read limits, and evaluation workers are bounded/configurable. Full source envelopes and results remain in memory only for active files; memory depends on configured file/worker limits, unit nesting, and question count, not merely source byte size.

Module ownership in `src/jevscan/`:

| Module | Responsibility |
| --- | --- |
| `core/models.py` | Lexical units, analytical targets, findings, coverage diagnostics, summary |
| `core/rules.py`, `config.py` | Question/report contracts; safe YAML loading and operational settings |
| `core/discovery.py`, `languages.py`, `parser.py` | Source discovery and lexical extraction |
| `core/context.py` | Exact evidence envelopes and coverage descriptions |
| `core/planning.py` | Target/question bindings, request packing, size-recovery decisions |
| `core/protocol.py`, `client.py` | Wire contracts and response validation; HTTP, pacing, retries |
| `core/evaluation.py` | File-local execution, answer attribution, severity classification, result lifecycle |
| `core/cache.py`, `scanner.py` | Raw-answer persistence; bounded pipeline/resource orchestration |
| `cli/args.py`, `main.py`, `render.py`, `terminal.py` | Arguments/entry point; report formatting; safe width-aware terminal output |

## API and privacy

Live requests go to `POST https://api.typesafe.ai/v1/systemone`; source and bounded context leave the machine. Authentication is read only from `TYPESAFE_API_KEY`. Model precedence is CLI `--model`, then `TYPESAFE_DEFAULT_MODEL`, then YAML. `TYPESAFE_BASE_URL` is an environment-only origin override; repository YAML cannot redirect the API key. HTTPS is required except for loopback tests, and redirects are disabled.

The SQLite cache stores raw answers, not submitted source or credentials. Keys include endpoint, request body, package version, and prompt version. Reports include declaration metadata and should also be treated as potentially sensitive. A moving model alias can reuse cached responses until expiration; pin a model for reproducibility. `--no-cache` disables both cache reads and writes.

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
