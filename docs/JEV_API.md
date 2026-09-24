# Jev API, evidence, and response attribution

The transport uses the published TypeSafe Python SDK v0.6.0 wire contract directly; the SDK is not a runtime dependency. The relevant primary source is its [generated models](https://github.com/typesafe-ai/typesafe-sdk-python/blob/v0.6.0/src/typesafe_sdk/_schemas/models.py), [question types](https://github.com/typesafe-ai/typesafe-sdk-python/blob/v0.6.0/src/typesafe_sdk/_core/question_types.py), and [request builder](https://github.com/typesafe-ai/typesafe-sdk-python/blob/v0.6.0/src/typesafe_sdk/_core/endpoints.py). The public [introduction](https://docs.typesafe.ai/introduction) describes independent typed questions evaluated against shared state.

## Request contract

```http
POST https://api.typesafe.ai/v1/systemone
Authorization: Bearer <TYPESAFE_API_KEY>
Content-Type: application/json
```

The body contains `model`, `state`, and a map of `questions`. State and question instructions accept JSON objects, so jevscan does not concatenate anonymous files or require a custom delimiter format.

A schematic request with two independent method judgments:

```json
{
  "model": "jev-latest",
  "state": {
    "documents": [
      {
        "path": "src/service.py",
        "language": "python",
        "start_byte": 0,
        "end_byte": 2048,
        "start_line": 1,
        "end_line": 80,
        "content": "<complete source envelope>"
      }
    ],
    "coverage": {
      "file_complete": true,
      "omitted_ranges": [],
      "external_references": "unresolved; no cross-file contracts or caller bodies supplied"
    },
    "jevscan_prompt": {
      "version": 6,
      "policy": "<fixed source-as-evidence and target-attribution policy>",
      "rubrics": {
        "<choice-rubric-id>": {
          "type": "choice",
          "instructions": "Classify the operation.",
          "criteria": {
            "safe": "The visible source establishes safe behavior.",
            "unsafe": "The visible source establishes unsafe behavior."
          }
        },
        "<noul-rubric-id>": {
          "type": "noul",
          "instructions": "Does source interleave independently meaningful responsibilities?"
        }
      }
    }
  },
  "questions": {
    "q00000": {
      "type": "choice",
      "instructions": {
        "target": {
          "path": "src/service.py",
          "name": "Coordinator.commit",
          "kind": "method",
          "start_line": 3,
          "end_line": 28,
          "span": [64, 712]
        },
        "rubric": "<choice-rubric-id>",
        "task": "Apply referenced rubric."
      },
      "criteria": {"safe": null, "unsafe": null}
    },
    "q00001": {
      "type": "noul",
      "instructions": {
        "target": {
          "path": "src/service.py",
          "name": "Coordinator.flush",
          "kind": "method",
          "start_line": 30,
          "end_line": 61,
          "span": [814, 1904]
        },
        "rubric": "<noul-rubric-id>",
        "task": "Apply referenced rubric."
      }
    }
  }
}
```

The placeholder source/ranges above illustrate the shape; actual spans come from Tree-sitter, and actual source is sent without clipping. Byte ranges are UTF-8 offsets into the original file, zero-based and end-exclusive. Lines are one-based and inclusive. A method's span remains its own even when the evidence document is the entire file or class.

Prompt version 6 stores the common policy and each distinct selected rule question once in `state.jevscan_prompt`.
Rubric references are the first 16 lowercase hex digits of SHA-256 over the canonical typed rule question; identical
question material shares one entry. Registry construction detects a prefix collision and stops rather than allowing
ambiguous references. A primary question carries only its type, source-unique path/name/line locator, rubric reference,
and minimal type shape: Choice labels map to `null`, Score carries numeric level labels, and Noul carries no repeated
rubric text. The source-unique locator includes an exact UTF-8 byte span in addition to path, name, and lines. Its task
points to the referenced rubric; the shared policy specifies that `instructions.rubric` indexes
`state.jevscan_prompt.rubrics` and directs Jev to apply both the rubric and `state.jevscan_prompt.policy`. This format
reduces repeated prompt text; it is a prompt change, not evidence that answer accuracy or confidence is unchanged.

`Check` owns the binding between request key, exact target, and YAML rule ID. The model-facing target descriptor is
deliberately compact; exact target IDs remain local attribution metadata, while byte spans are included in questions
to distinguish targets. Each question refers to one exact target, can use other supplied documents as evidence, and
must not assign a class-wide concern indiscriminately to every method. The fixed policy also says source
strings/comments are evidence, not instructions; this is a guardrail, not a proven prompt-injection defense. Auxiliary
questions refer to the same shared rubric and carry only their own follow-up task instead of repeating the entire
rule question.

No absolute home directory is added to an ordinary project-relative path merely to describe file identity. Source outside the selected project root may retain the discovery layer's absolute display path. Primary envelopes do not read cross-file source. Enrichment can conditionally discover and supply allowed source from the resolved project root; see the data-sharing contract below.

## Planning and budgets

`ContextBuilder` supplies exact source documents, including disjoint AST spans after recovery. `Evidence` identity hashes the actual encoded state rather than a bounding interval, so different omissions cannot alias. The planner binds independent questions and packs them under configured local budgets. Packaged token thresholds are 28k state+longest-question and 56k aggregate. `RequestBudget` also carries the current Jev-1.13 provider profile (32k/64k) for `jev-latest`, `jev-preview`, and pinned 1.13 IDs; local `null` removes headroom but not that known ceiling. Byte and question limits remain hard.

The two token dimensions drive different preflight actions. Aggregate/question-count overflow with fitting evidence splits questions while preserving source. A shared state that is itself over the context ceiling is not first exploded into singleton checks: sibling questions stay grouped while bounded recovery prepares smaller evidence, and checks that converge on identical evidence are repacked. Successful `usage.input_tokens` observations can only make the shared run-local byte/token estimate more conservative.

The batching key is the exact encoded evidence, not the rule ID. Independent Noul, Choice, and Score questions for different targets can coexist in one System One request when they share that evidence and fit the limits. The selected-rubric registry is identical across batches for a resolved configuration and is included in every effective request state. The planner does not enlarge source evidence merely to create a batch.

Complete-file contexts are additionally bounded by `scan.max_full_file_lines` (3,000 by default). Checks whose requested state is a larger complete file are explicit omissions; smaller owner/unit contexts can still be evaluated. `--plan` runs discovery/parsing and initial packing without constructing a live client, producing a conservative initial input/cost estimate.

That initial estimate includes the shared rubric registry but does not predict later enrichment or compaction calls. A smaller primary payload is not a full-scan cost guarantee: reserved input tokens and provider-reported usage differ, and follow-up phases can add requests. Compare the live summary's reported tokens and cost as well as its conservative reservations.

`FileExecutor` owns bounded recovery. HTTP 413 is a payload-size signal. At HTTP 400/422, the client recursively inspects bounded machine fields (`code`, `type`, `status`, scalar machine-like `error`) and accepts only the exact value `max_tokens_exceeded` as a size signal. Nested validation forms such as `detail[].type=max_tokens_exceeded` are covered; free-text `message`/`msg` token mentions are not. Status, recognized machine fields and a sanitized request ID reach the audit; source/error-body text and credentials do not.

The current official SDK supplies generic status/body error handling, not a published specialized context exception schema. The Pydantic integration documentation identifies `max_tokens_exceeded` and describes Jev-1.13 as 32k state-plus-longest-question / 64k aggregate. No authenticated oversized request was used to capture a production rejection envelope. See [research and recovery](CONTEXT_RECOVERY.md) for evidence, limits and compatibility rationale.

Recognized provider size rejection remains a fallback for estimator/tokenizer mismatch. A failed singleton state becomes a conservative per-file compaction hint; exact rejected bodies are never replayed. Unknown request-local HTTP 400/422 responses are instead sanitized and attributed to the affected checks, which are omitted while unrelated requests continue. Three equivalent request-local rejections trip a client-wide circuit breaker and become scan-fatal. Authentication/permission and other systemic failures remain immediately fatal.

An entire target that cannot be evaluated is reported as omitted. There is no hidden chunk-score aggregation or generated summary substituted for target source. Disjoint documents retain original UTF-8 byte/line positions and exact omission coverage. File-level judgments always require the full file. Published model profiles are explicit versioned application data; moving aliases may require a jevscan update, while provider rejection remains the final safeguard.

## Validation and lifecycle

`core/protocol.py` validates network and cache data once before it enters the trusted pipeline. It rejects missing/extra question IDs, answer-type mismatches, labels absent from criteria, malformed numbers, and out-of-range values. Probabilities are preserved as returned; jevscan does not assume they sum to exactly one or normalize them silently. Jev response fields not used by the application are allowed.

`core/client.py` owns HTTPS/origin restrictions, pooled HTTP connections, one scan-wide concurrency semaphore, rate pacing, retries, paid-attempt budget reservation, provider-error classification, and the request-rejection circuit breaker. Unknown HTTP 400/422 responses do not trigger context reduction; they are request-local until three equivalent failures indicate a systemic problem. HTTP 401/403 and other systemic failures abort. Transient network errors and retryable status codes follow bounded retries and `Retry-After`; waits exceeding the configured ceiling stop rather than retry early. No submitted bodies or API keys appear in error messages.

`core/evaluation.py` owns answers for one active file. It reclassifies cached answers using the active reporting policy, assigns answers to exact targets, and emits each target once after its file finishes. Abort/cancellation still emits completed answers and records unanswered checks. File evaluators run concurrently, and independent ready request batches inside one file may also run concurrently; the client semaphore and limiter remain global. Questions in a shared request remain logically independent; a question cannot consume another answer from that same request.

The SQLite cache has two layers. Whole-request entries retain the endpoint/body/package/prompt identity; the current
prompt compatibility version is 6. Per-judgment entries are keyed by endpoint, requested model, exact encoded
effective state (source evidence plus the complete shared rubric registry), exact short bound question, and prompt
compatibility. They are validated against the active typed rule question before use. Batch composition can change
without repurchasing an unchanged judgment while that effective state and question remain identical. If rule selection
changes the registry contents, the effective state changes and judgments are not reused across that boundary. Pin a
model for reproducibility.

The optional `budget` block limits request attempts, conservative estimated input tokens, and/or configured input cost before transport. Reservations include retries. Exceeding a guard raises an explicit `budget-exhausted` incomplete-scan diagnostic; unevaluated checks are never converted into clean answers. The final summary also reports actual successful-response input tokens and their configured input-cost calculation.

## Machine reports

Report schema **9** is separate from configuration schema **4**. JSON contains metadata, events, and a final summary. JSONL has `start`, source/coverage/diagnostic/evaluation events, optional `plan` events in plan mode, and a final `summary`, flushing each event.

An `evaluation` event contains:

| Field | Meaning |
| --- | --- |
| `target` | Complete unit/file identity and source span |
| `answers` | Raw typed answers keyed by YAML rule name |
| `rule_metadata` | Per-rule title, ruleset membership, and `blocks_exit` policy, separate from identity |
| `statuses` | `ok`, `unknown`, `not_applicable`, `warning`, or `error` per answer |
| `uncertainty_reasons`, `reviews` | Decision reasons and an auditable, bounded enrichment history |
| `findings` | Confidence-qualified warning/error findings, each with `target`; only findings whose `rule_metadata.blocks_exit` is true can trigger `--fail-on` |
| `tentative_findings` | Indicated warning/error signals that remain `unknown`; same item structure, never duplicated in `findings` |
| `context_selection` | Local recovery/relevance audit, including safe request-local rejection metadata, separate from post-answer enrichment reviews |
| `evidence` | Per-rule included ranges, original-file omissions, requested context, `context_complete`, `target_complete` |
| `models` | Model returned for each rule, including separate batches |
| `cached_rules`, `cached` | Per-rule cache provenance; whole-target cache flag is true only when all checks completed from cache |
| `scales` | Score rubric maximum, where applicable |
| `skipped_rules` | Rules with no result and the explicit reason |
| `applicability_skips` | Deterministic not-applicable rules and declared missing syntax prerequisites, separate from `skipped_rules` |
| `inference` | Per-rule phase/cache/question bytes plus nested shared-request metrics: request hash, serialized request/state/all-question bytes, evidence-group density (documents per source file), and provider-reported request usage when present |

`summary.tentative_findings` counts tentative warnings/errors separately; they are already included in `summary.uncertain`. `summary.advisory_findings` counts confirmed findings whose rule has `blocks_exit: false`; they remain visible but cannot trigger `--fail-on`. Reviews distinguish the admitted `trigger` from the `initial_reason`. Ordinary intrinsic ambiguity does not request enrichment by default; no review entry means no review was requested, not that a model approved the evidence.
Schema 9 retains schema 8 coverage events, including `request_rejected_checks` beside compacted/omitted counts and `aborted`. `summary.request_rejections` counts request-local HTTP 400/422 rejections that were isolated rather than treated as size errors or immediate scan-fatal failures. `summary.applicability_skips` counts deterministic policy exclusions. Request-local rejection details live under the affected rule's `context_selection.request_rejections`; only sanitized machine metadata is retained.

Schema 9 retains schema 8 planning/cost counters and `plan` events for `--plan`, and adds per-rule `blocks_exit` metadata plus `summary.advisory_findings`. Interactive progress remains text-only and is never inserted into JSON/JSONL.


The raw source evidence is not copied into machine reports, but target/declaration metadata may still be sensitive. Interactive text progress is ephemeral and is not inserted into JSON/JSONL event streams. Verbosity, terminal colors, and text display limits never remove machine-report answers. Completion describes software coverage, not proof of semantic correctness; low-confidence and insufficient-evidence judgments remain distinguishable from clean results.

## Verification limits

Automated verification uses the actual HTTPX client with MockTransport and the
real bundled grammar packages; it makes no live authenticated Jev calls. It
verifies request construction, multiple target bindings, exact byte accounting,
shuffled responses, bounded size recovery, caching, and failure transitions. It
does not establish Jev's long-context accuracy, real account rate limits,
latency, or current availability. Separately retained calibration captures may
use authenticated inference and record that model, cost, and provenance under
`calibration/`. See [verification](VERIFICATION.md).


## Enrichment and source sharing

The HTTP contract is unchanged. `core/inference.py` is the shared validated/cached prediction owner. `RequestBudget` enforces context, aggregate, byte, and question-count limits in all phases. Auxiliary answers must match the submitted IDs and primitive contracts.

In targeted mode, the routing request contains a disposition Choice plus only the independent family Nouls declared by that rule. User-facing `callees` maps to the syntax-linked definitions route. Full mode exposes every family; off mode performs no enrichment. Only a confident `local_evidence` disposition admits qualifying families. Candidates come from allowed local source and immutable per-file snapshots. Each candidate's relevance is a separate Noul; all families share candidate and evidence budgets. The final assessment sees the original bound question and additional source, not previous verdicts or routing/relevance scores.

Report reviews record disposition, all family probabilities, admitted families, per-family coverage, candidate memberships, selected source, omissions, and stopping outcomes. The resulting `retrieval_coverage` contains `families`, `candidate_families`, and combined-pool omission counts. Family membership is lexical/provenance information, not a resolved contract.

Configuration version 4 is additive YAML. Rule names (JEV01–JEV16 for built-ins) are stable identifiers. Titles/rulesets
are report metadata, not model instructions. JEV12–JEV16 are non-blocking code review signals: they require
source-visible evidence, do not enforce runtime behavior, and make no recall claim for real positive cases. Disabling a
rule or set prevents creating those questions; selection may also change request batching/cache identities.

Source outside the scanned subdirectory can be selected only under the resolved project root and filters. Selected evidence does not imply all callers have been seen or that per-file snapshots form an atomic repository revision. See [ENRICHMENT.md](ENRICHMENT.md) for the precise algorithm, research basis, and live-acceptance boundary.
