# jevscan

A configurable semantic code-quality scanner for **Python, Rust, Perl, TypeScript, and JavaScript**.

Tree-sitter finds lexical code units. Jev evaluates the applicable YAML rules. jevscan applies the reporting thresholds and produces a Rich terminal report or streaming JSON/JSONL. It does not execute or import the code being scanned.

**Status: initial implementation.** See [verification](docs/VERIFICATION.md) for the exact checks performed and the integration checks not run in the delivery environment. Default semantic thresholds are starting points, not calibrated accuracy guarantees.

## Install and run

Python 3.12 or newer is required.

```bash
cd jevscan
uv sync

# Parsing inventory: functions, classes, methods, and other supported units.
# No API key, network requests, or response-cache writes.
uv run jevscan /path/to/project --offline

# Semantic analysis sends selected code and bounded local context to TypeSafe.
export TYPESAFE_API_KEY='your-key'
uv run jevscan /path/to/project
uv run jevscan /path/to/project/src/service.py
```

The distribution is named `py3-jevscan`; the import package and command are `jevscan`. To install an included or locally built wheel:

```bash
uv tool install ./dist/py3_jevscan-0.1.0-py3-none-any.whl
```

A dependency installation needs access to a Python package index. Grammar/runtime versions are deliberately pinned together in `requirements.txt`: `tree-sitter==0.25.2` and the bundled-grammar `tree-sitter-language-pack==0.13.0`. The scanner does not fetch grammars during a scan. Updating these pins requires rerunning the parser integration tests.

## Configuration

From the project where you want a configuration:

```bash
uv run jevscan --init-config                 # creates jevscan.yaml; never overwrites
uv run jevscan --show-config                 # resolved settings, including CLI overrides
uv run jevscan --list-rules
uv run jevscan src --config quality.yaml
```

Resolution is: explicit `--config`, then the nearest `jevscan.yaml`/`jevscan.yml` walking upward from the **working directory**, then the targets' nearest configuration if none was found from the working directory, then the wheel's packaged default. Upward search stops at a Git worktree boundary. Multiple target configurations without a working-directory override are rejected; scan those projects separately or specify `--config`.

A discovered configuration applies to the whole invocation. jevscan does not switch rule sets for nested directories mid-scan. Having both `jevscan.yaml` and `jevscan.yml` at the same level is an error.

**A custom file replaces the default rule set.** Operational settings omitted from it receive schema defaults. Add `extends: default` explicitly to inherit packaged rules and override selected fields:

```yaml
version: 1
extends: default

scan:
  jobs: 8
jev:
  concurrency: 24
  requests_per_minute: 600

rules:
  redundant-validation:
    report:
      min_probability: 0.95
  unclear-control-flow:
    enabled: false
```

Mappings merge recursively only when extending; lists replace, rather than append. Unknown settings, duplicate YAML keys, invalid question/report combinations, and out-of-range thresholds fail before scanning. [Full rule guide](docs/CONFIGURATION.md) · [standalone example](examples/standalone.yaml).

## Code units

| Language | Extracted units |
| --- | --- |
| Python | Functions, async functions, classes, nested classes, instance/static/class methods. Decorators are included in the unit's source span. |
| Rust | Functions, structs, enums, traits, trait declarations/default methods, impl blocks and methods, modules, type aliases, closures. |
| TypeScript | Functions, classes, constructors/methods, assigned arrows, interfaces and method signatures, type aliases, namespaces, abstract classes/methods. TSX is supported. |
| JavaScript | Functions, classes, constructors/methods, assigned arrows/function expressions, anonymous closures and object methods. JSX is supported. |
| Perl | Subroutines, lexical package ownership, native classes and explicit methods, anonymous subroutines. Both `.pl` and `.pm` are included, as are `.t` test files. |

Every unit has a qualified name, parent ID, UTF-8 byte range, line range, signature, and kind. A method can be evaluated separately from its whole class or impl. Ordinary Perl `sub` declarations remain `function` units within their package: lexical parsing cannot prove whether a sub is called as an OO method. Both callable kinds receive the default callable rules. Runtime-generated Moose/Moo methods and dynamic metaprogramming are not resolved.

Tree-sitter is a syntax parser, not a compiler or a whole-program analyzer. Grammar limitations and parse errors are reported as incomplete coverage, not silently accepted as clean code. Source must be UTF-8. Branch-node counts are structural observations, **not** a language-independent cyclomatic-complexity score.

## Reports and scope

```bash
uv run jevscan src --rule mixed-responsibilities
uv run jevscan . --format jsonl -o findings.jsonl
uv run jevscan . --format json -o findings.json
uv run jevscan . --jobs 8 --concurrency 32 --rpm 900
uv run jevscan . --no-cache --fail-on error
uv run jevscan . --offline --format jsonl -o inventory.jsonl
```

Rich output has a live progress indicator, finding panels with qualified names and line ranges, and a final coverage summary. Terminal details are limited to 100 by default (`--max-display 0` removes this limit); diagnostics remain visible, and JSON/JSONL are never display-limited. Machine reports include unit metadata, typed answers, findings, diagnostics, and a final summary. Reports contain names and declaration signatures; treat them as potentially sensitive.

`model P` is the model's assigned probability, not a measured probability that a finding is correct. Severity comes from the rule, not from confidence. Score thresholds use the configured rubric's zero-based scale. Findings currently refer to the **whole extracted unit**; there is no second-pass line localization or generated repair advice.

The default rules address mixed responsibilities, control-flow clarity, abstraction levels, redundant validation, hidden invariant failures, decomposition, ownership, cohesion, and duplicated behavior. Context is bounded **same-file declarations and imports**, not resolved caller bodies or a repository-wide call graph. Rules that need an upstream contract have an explicit insufficient-context option. No whole-repository architecture, duplication, or test-coverage claim is inferred from a local snippet.

Exit status:

- `0`: analysis completed without findings at the selected severity, or offline inventory completed.
- `1`: findings meet `--fail-on` (default: `warning`).
- `2`: invalid configuration, an incomplete scan, an operational failure, or a resource-limit omission.
- `130`: interrupted with Ctrl-C.

`--fail-on never` does not hide incomplete coverage. Units with no applicable rules are counted as skipped by configuration; this alone is not an operational error. A successful semantic scan is not proof of code correctness.

## Parallelism and resource bounds

Discovery runs off the event loop. A fixed number of **spawned process workers** perform native Tree-sitter parsing and normalization in file batches. A bounded queue feeds fixed async evaluator workers using one HTTPX connection pool. The coordinator does not create a future for every file or retain all repository findings.

Defaults: up to 8 parser processes, batches of 8 files, 64 queued evaluations, and 16 API slots. CPU and network stages overlap. These are adjustable operating defaults, **not benchmark-derived claims about optimal throughput**. Directory traversal retains open iterators by depth rather than every sibling directory. SQLite reads/writes use a dedicated thread. JSON output is incremental; JSONL is useful for large or interrupted runs.

`--rpm` paces **attempts**, including retries; concurrency controls in-flight work, not account quota. The default 600 attempts/minute is conservative configuration, not a statement of TypeSafe's limits. Rate-limit responses share a cooldown, `Retry-After` is respected, and exhausted API failures abort the scan. There is no separate token-per-minute limiter; choose limits for your account and workload.

File, unit, context, request-byte, question-count, and queue limits are configurable. Oversized source units are skipped explicitly rather than silently truncated; smaller nested methods may still be evaluated. Request-byte limits are not a tokenizer or a guarantee against provider token limits. Memory is bounded by configured batches/queues and per-file limits, not by repository size; raising all limits together can still use substantial memory.

Nested `.gitignore` files and configured Git-style include/exclude patterns are respected. Symlinks are not followed. Global Git ignore files and `.git/info/exclude` are not loaded. Explicit files still obey filters and produce a diagnostic when excluded. Minified JavaScript, dependencies, build artifacts, and common virtual environments are excluded by default.

## API, cache, and privacy

The live client sends `POST /v1/systemone` to `https://api.typesafe.ai`, with multiple typed questions per code unit. It uses the verified wire contract directly rather than depending on an SDK wrapper. See the [API guide and protocol references](docs/JEV_API.md).

`TYPESAFE_API_KEY` is read only from the environment. Model precedence is `--model`, `TYPESAFE_DEFAULT_MODEL`, then YAML. `TYPESAFE_BASE_URL` is a trusted-environment override for a compatible endpoint; a repository YAML file cannot redirect the API key. Redirects are disabled. Review project rule configurations before running them against sensitive code.

Cache keys include the endpoint, request model, complete submitted state/questions, and jevscan's version. The local SQLite cache stores answers, not submitted source text or API keys, with a default 24-hour TTL. Reporting thresholds can change without repeating an unchanged question. `--no-cache` disables reads and writes. A moving model alias can retain cached answers until expiry: use a specific available model ID for reproducible evaluation. Deleting `.jevscan-cache/` removes the cache.

Retries after a connection failure can duplicate a prediction request and its cost. The tool cannot guarantee provider-side cancellation or that requests already in flight stop being billed after local cancellation. It does not execute scanned code, but it is not a sandbox for hostile filesystems or a guarantee against prompt injection.

## Development and build

```bash
uv sync
uv run pytest -q -rs
uv run pytest -m parser -q                # real grammars and spawned-process pipeline
uv run ruff format src tests
uv run ruff check src tests
uv run ty check
uv build
```

Production layout:

```text
src/jevscan/
├── __init__.py
├── __main__.py
├── cli/
│   ├── args.py
│   ├── main.py
│   └── render.py
├── core/
│   ├── models.py
│   ├── config.py
│   ├── languages.py
│   ├── parser.py
│   ├── discovery.py
│   ├── context.py
│   ├── client.py
│   ├── cache.py
│   └── scanner.py
└── data/default.yaml
```

Tests exercise the production owners. HTTP tests use HTTPX's mock transport; parser integration tests use the real grammar packages and are marked separately. No API key is required for the automated test suite.
