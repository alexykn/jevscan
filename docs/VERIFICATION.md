# Verification record — 18 September 2026

## Executed locally

Environment: Linux x86-64, CPython **3.13.5**. Dependencies already present included HTTPX, Pydantic, PyYAML, pathspec, pytest, pytest-asyncio, and setuptools 82.0.1.

```text
PYTHONPATH=src pytest -q -rs --disable-warnings
43 passed, 13 skipped

python -m compileall -q src tests
passed
```

The passing tests exercise production code for:

- Packaged defaults, project discovery and replacement, explicit inheritance, duplicate keys, invalid schemas, and endpoint/cache-path restrictions.
- File filters, nested Git ignore behavior, overlapping targets, symlink handling, lazy traversal of wide trees, and directory-iterator cleanup.
- The real HTTPX client using MockTransport: endpoint, bearer header, body schema, transient failures, rate-limit handling, response validation, rubric gates, and request budgets.
- Bounded worker/task concurrency and streamed events in an 80-file pipeline fixture, response-cache reuse, offline behavior, and fail-fast/cancellation transitions.
- CLI configuration commands, output protection, JSON/JSONL structure, and exit status.

The pipeline fixture supplies synthetic parsed files to isolate queue/cache/HTTP behavior. It does **not** establish correctness of Tree-sitter extraction or measure parser throughput. Concurrency assertions are not massive-codebase benchmarks.

## Wheel checks

A wheel was built using the installed setuptools backend, not by manually constructing its files:

```python
from setuptools.build_meta import build_wheel
build_wheel("dist")
```

Verified the wheel's entry point, version/Python/dependency metadata, complete RECORD hashes, and inclusion of `jevscan/data/default.yaml`. Ran `--version`, `--show-config`, and `--list-rules` from a temporary directory with only the wheel on PYTHONPATH, outside the source checkout. Both example configurations validated successfully.

## Not executed

**Thirteen real-parser integration cases were skipped** because `tree-sitter` and `tree-sitter-language-pack` could not be installed in this network-restricted container. They cover all five languages plus JSX/TSX, native Perl classes, package-scope restoration, byte/line spans, syntax/read/size errors, and the production spawned-process pipeline. The adapter implementation therefore has not been validated against running native grammars here.

Also not executed:

- A live authenticated Jev call: no API key was available. Request/response schemas were checked against the published SDK source and mocked at the transport boundary.
- Python 3.12 or macOS/Apple Silicon execution.
- Ruff formatting/lint checks or ty checks: those executables were unavailable and could not be downloaded. Their configuration is included, but a clean result is **not claimed**.
- Large-repository throughput, peak-memory, provider quota, or semantic accuracy/false-positive benchmarks.
- A dependency-resolved `uv sync` or `uv build`. The wheel build used already installed setuptools directly; no `uv.lock` was fabricated.

## Run with dependencies available

```bash
uv sync
uv run pytest -q -rs
uv run pytest -m parser -q -rs
uv run ruff format src tests
uv run ruff check src tests
uv run ty check
uv build

# Real parsing with no provider charges:
uv run jevscan tests/fixtures --offline --format jsonl -o inventory.jsonl

# Optional authenticated smoke test; sends this file to TypeSafe:
export TYPESAFE_API_KEY='your-key'
uv run jevscan tests/fixtures/sample.py --no-cache --fail-on never
```

A full dependency installation should run the parser tests rather than skip them. Resolve any failures before treating this initial implementation as a release. Calibrate semantic rules against labelled examples before using them as mandatory CI gates.
