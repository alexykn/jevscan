# Rule configuration

The packaged `jevscan/data/default.yaml` is the full default configuration. Generate a project copy with `jevscan --init-config`. Editing that copy needs no Python changes or rebuild.

## A standalone rule set

Without `extends: default`, these are the only rules that run:

```yaml
version: 1
rules:
  mixed-work:
    applies_to: [function, method, closure]
    languages: [python, rust, perl, typescript, javascript]
    require_body: true
    question:
      type: noul
      instructions: >
        Does `source` interleave unrelated responsibilities in a way
        that makes this operation materially harder to understand?
        Inherent domain complexity alone is not evidence of a problem.
    report:
      severity: warning
      message: Several responsibilities appear interleaved in this operation.
      min_probability: 0.90
```

Supported kinds: `function`, `method`, `closure`, `class`, `struct`, `enum`, `trait`, `impl`, `interface`, `type`, `module`, `package`. TSX uses language `typescript`; JSX uses `javascript`.

`enabled` defaults to true. `languages` defaults to all five. `require_body` defaults to false; enable it for checks that need an implementation rather than a declaration. `applies_to`, `question`, and `report.message` are required. IDs contain lowercase letters, digits, hyphens, or underscores.

## Question and report forms

### Noul

The probability is for an affirmative answer. `report.expected: false` instead gates on `1 - noul`.

```yaml
question:
  type: noul
  instructions: Is the control flow in `source` straightforward to follow?
report:
  message: The control flow appears hard to follow.
  severity: warning
  expected: false
  min_probability: 0.90
```

Noul does not use a separate confidence threshold. jevscan does not support optional Noul criteria descriptions in this initial schema; put a focused distinction in `instructions` or use Choice.

### Choice

Define mutually distinguishable options, including an uncertainty option when evidence can be missing. The selected option must appear in `report.choices`, and its probability must meet `min_probability`. An optional `min_confidence` gate uses the provider's separate confidence field.

```yaml
question:
  type: choice
  instructions: >
    Does `source` repeat validation of an invariant whose guarantee
    is explicitly established in the supplied evidence? Do not infer
    guarantees from names, type hints alone, or omitted caller code.
  criteria:
    redundant: The supplied evidence explicitly establishes the unchanged invariant.
    justified: The check guards a trust boundary, mutation, or a legitimate runtime failure.
    absent: No redundant validation is apparent.
    unknown: The evidence is insufficient to establish the upstream guarantee.
report:
  severity: warning
  message: This check appears to repeat an explicitly established invariant.
  choices: [redundant]
  min_probability: 0.95
  min_confidence: 0.70
```

### Score

The ordered criteria correspond to `0, 1, ...`. The answer is an expected score and need not be an integer. Choose exactly one of `min_score` or `max_score`.

```yaml
question:
  type: score
  instructions: How difficult is the main execution path in `source` to follow?
  criteria:
    - Straightforward, with complexity justified by the operation.
    - Mostly clear, with limited local indirection.
    - Several competing concerns substantially obscure the main path.
    - The main path is tangled and difficult to establish.
report:
  severity: warning
  message: The main execution path appears difficult to follow.
  min_score: 2.4
  min_confidence: 0.70
```

A Score report cannot use `min_probability`, `choices`, or `expected: false`. Default threshold values are heuristic starting points: evaluate precision and false positives on labelled examples from your languages before using semantic findings to block changes.

## State visible to every question

```text
language
unit: kind, qualified name, path, start/end line
source: the complete selected unit
context: bounded parent/member/sibling declarations and imports
context_scope: an explicit description of the limited evidence
context_omitted: whether the context budget omitted candidates
```

There is no resolved caller graph. Each question is independent; one answer cannot feed another question in the same request. The program adds a fixed instruction to treat source comments and strings as evidence, not instructions. This is a guardrail, not a proven prompt-injection defense.

## Operational settings

`jevscan --show-config` lists every setting and its resolved value. Key groups:

| Group | Settings |
| --- | --- |
| `scan` | `include`, `exclude`, `respect_gitignore`, `jobs`, `batch_size`, `queue_size`, `max_file_bytes`, `max_units_per_file`, `max_unit_bytes`, `context_bytes`, `context_members` |
| `jev` | `model`, `concurrency`, `requests_per_minute`, `timeout_seconds`, `retries`, `max_retry_delay`, `max_request_bytes`, `max_questions` |
| `cache` | `enabled`, project-relative `path`, `ttl_seconds` (`0` means no expiration) |

All sizes are bytes, not tokens. A file exceeding a limit is an explicit coverage omission, not a lint finding. Cache paths cannot be absolute or contain `..`. Authentication and endpoint selection are intentionally not project-YAML settings.

`include` and `exclude` are Git-style path patterns, not `Path.glob` syntax. Patterns such as `*.py` match that basename at any depth; a leading `/` anchors to the scan's resolved project root. Negative patterns are handled by pathspec. As with Git, an ignored parent directory is pruned before its descendants can be re-included.

With `extends: default`, mappings merge recursively and lists replace. To change a question's primitive, replace the entire rule in a standalone configuration rather than retaining incompatible inherited fields. To disable a packaged rule, set `enabled: false`.
