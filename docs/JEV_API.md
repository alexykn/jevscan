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
    }
  },
  "questions": {
    "q00000": {
      "type": "noul",
      "instructions": {
        "policy": "<fixed source-as-evidence and target-attribution policy>",
        "target": {
          "id": "src/service.py:50:method",
          "scope": "unit",
          "path": "src/service.py",
          "language": "python",
          "qualified_name": "Coordinator.commit",
          "kind": "method",
          "start_byte": 50,
          "end_byte": 800,
          "start_line": 3,
          "end_line": 28
        },
        "task": "Does source interleave independently meaningful responsibilities?"
      }
    },
    "q00001": {
      "type": "noul",
      "instructions": {
        "policy": "<same fixed policy>",
        "target": {
          "id": "src/service.py:810:method",
          "scope": "unit",
          "path": "src/service.py",
          "language": "python",
          "qualified_name": "Coordinator.flush",
          "kind": "method",
          "start_byte": 810,
          "end_byte": 1600,
          "start_line": 30,
          "end_line": 61
        },
        "task": "Does source interleave independently meaningful responsibilities?"
      }
    }
  }
}
```

The placeholder source/ranges above illustrate the shape; actual spans come from Tree-sitter, and actual source is sent without clipping. Byte ranges are UTF-8 offsets into the original file, zero-based and end-exclusive. Lines are one-based and inclusive. A method's span remains its own even when the evidence document is the entire file or class.

`Check` owns the binding between request key, target, and YAML rule ID. Each question says that `source` in its instructions/criteria means only that target. It can use the rest of the supplied document as evidence, but must not assign a class-wide concern indiscriminately to every method. The fixed policy also says source strings/comments are evidence, not instructions; this is a guardrail, not a proven prompt-injection defense.

No absolute home directory is added to an ordinary project-relative path merely to describe file identity. Source outside the selected project root may retain the discovery layer's absolute display path. Primary envelopes do not read cross-file source. Enrichment can conditionally discover and supply allowed source from the resolved project root; see the data-sharing contract below.

## Planning and budgets

Requested file/owner/unit source is attempted intact when it fits the mandatory serialized-byte and question-count caps, plus any explicitly configured token-estimate caps. Defaults no longer use the heuristic 28k estimate as a preemptive source cutoff. TypeSafe's Models page, retrieved on 2026-09-18, specifies **32k state-plus-longest-question and 64k aggregate tokens for Jev 1.13**; the SDK model-list schema itself does not provide a tokenizer or numeric capacity field. `null` local estimates do not remove those real service limits.

`RequestBudget` owns exact wire byte accounting and the UTF-8 heuristic for every phase. Normal and prepared requests sharing identical evidence can be packed; each question retains its target/rule binding. `Evidence.key` hashes the complete encoded state, so two selections with equal bounding offsets but different included ranges are not conflated.

Size recovery first bisects question batches. A singleton unit can enter bounded AST-aware preparation; a singleton file cannot be partly scored. Preparation reduces budgets geometrically, then may try the complete target alone. It never repeats an identical rejected primary body. A file-local record of singleton state rejection avoids redundant large probes for other unit checks, with an explicit preparation cause rather than an invented individual provider rejection.

### Errors: documented contract versus compatibility handling

The public [API](https://docs.typesafe.ai/api), [SDK exceptions](https://docs.typesafe.ai/sdk/python/api/exceptions), and [OpenAPI schema](https://api.typesafe.ai/openapi.json) were retrieved during this release's research. They document HTTP validation failures (422), authentication/permission failures, rate limiting (429), and server/overload handling (including 529). The OpenAPI 422 shape is `detail: [{loc, msg, type, input?, ctx?}, ...]`. **They do not specify a canonical token-limit response body.** No authenticated size-rejection response was observed for this release.

The conservative compatibility handler recognizes HTTP 413, and at HTTP 400/422 only an exact `max_tokens_exceeded` code in a top-level object or an `error`/`detail` object. For example, this is a *supported compatibility shape*, not a claim about a documented real rejection sample:

```json
{"error": {"code": "max_tokens_exceeded"}}
```

Arbitrary 422 validation lists, prose mentioning context, unknown codes, authentication failures and timeouts are **not** interpreted as permission to remove source. Unknown errors fail with status/request ID for diagnosis. Echoed `input`, source text, arbitrary server messages, and API keys are never copied into rejection audits. Recognized rejection records retain bounded status, allowlisted code, sanitized request ID and request hash.

429, 529 and other retryable transport/server outcomes retain bounded backoff/`Retry-After` behavior; they do not trigger compaction. Explicit size failures do not retry the same request unchanged. See [CONTEXT.md](CONTEXT.md) for the finite recovery path and the source-completeness contract.

## Validation and lifecycle

`core/protocol.py` validates network and cache data once before it enters the trusted pipeline. It rejects missing/extra question IDs, answer-type mismatches, labels absent from criteria, malformed numbers, and out-of-range values. Probabilities are preserved as returned; jevscan does not assume they sum to exactly one or normalize them silently. Jev response fields not used by the application are allowed.

`core/client.py` owns HTTPS/origin restrictions, pooled HTTP connections, rate pacing, retries, and provider-error classification. Arbitrary HTTP 400/401 responses do not trigger context reduction. Transient network errors and retryable status codes follow bounded retries and `Retry-After`; waits exceeding the configured ceiling stop rather than retry early. No submitted bodies or API keys appear in error messages.

`core/evaluation.py` owns answers for one active file. It reclassifies cached raw responses using the active reporting policy, assigns answers to exact targets, and emits each target once after its file finishes. Abort/cancellation still emits completed answers and records unanswered checks. File evaluators run concurrently; request batches within a file are sequential. Questions in a shared request remain logically independent; a question cannot consume another answer from that same request.

Raw-answer cache keys include endpoint, canonical request body, package version, and prompt version (currently 4). Threshold, severity-message, and uncertainty-policy changes do not alter the request. Model, question, target, source, or evidence changes do. A moving model alias can keep serving cache entries until expiry; pin a model for reproducibility.

## Machine reports

Report schema **6** is separate from configuration schema **4**. JSON contains metadata, events, and a final summary. JSONL has `start`, source/coverage/diagnostic/evaluation events, and a final `summary`, flushing each event.

An `evaluation` event contains:

| Field | Meaning |
| --- | --- |
| `target` | Canonical unit/file identity/source span plus a source-derived display label |
| `answers` | Raw typed answers keyed by YAML rule name |
| `rule_metadata` | Per-rule title and ruleset membership, separate from identity |
| `statuses` | `ok`, `unknown`, `not_applicable`, `warning`, or `error` per answer |
| `preparation` | Per-rule compaction/source selection and bounded rejection history before primary answers |
| `uncertainty_reasons`, `reviews` | Decision reasons and an auditable, bounded enrichment history |
| `findings` | Confidence-qualified warning/error findings, each with `target`; these alone determine `--fail-on` |
| `tentative_findings` | Indicated warning/error signals that remain `unknown`; same item structure, never duplicated in `findings` |
| `evidence` | Per-rule included ranges, original-file omissions, requested context, `context_complete`, `target_complete` |
| `models` | Model returned for each rule, including separate batches |
| `cached_rules`, `cached` | Per-rule cache provenance; whole-target cache flag is true only when all checks completed from cache |
| `scales` | Score rubric maximum, where applicable |
| `skipped_rules` | Rules with no result and the explicit reason |

`summary.tentative_findings` counts tentative warnings/errors separately; they are already included in `summary.uncertain`. Reviews distinguish the admitted `trigger` from the `initial_reason`. Ordinary intrinsic ambiguity does not request enrichment by default; no review entry means no review was requested, not that a model approved the evidence.

The raw source evidence is not copied into machine reports, but target/declaration metadata may still be sensitive. Verbosity, terminal colors, and text display limits never remove machine-report answers. Completion describes software coverage, not proof of semantic correctness; low-confidence and insufficient-evidence judgments remain distinguishable from clean results.

## Verification limits

Automated tests use the actual HTTPX client with MockTransport and the real bundled grammar packages. They verify request construction, multiple target bindings, exact byte accounting, shuffled responses, bounded size recovery, caching, and failure transitions. They do not establish Jev's long-context accuracy, real account rate limits, latency, or current availability. No live authenticated Jev call was made for this release candidate. See [verification](VERIFICATION.md).


## Enrichment and source sharing

The HTTP contract is unchanged. `core/inference.py` is the shared validated/cached prediction owner. `RequestBudget` enforces context, aggregate, byte, and question-count limits in all phases. Auxiliary answers must match the submitted IDs and primitive contracts.

The routing request contains a disposition Choice and four independent family Nouls, not a seven-way mutually exclusive route. Only a confident `local_evidence` disposition admits qualifying families. Candidates come from allowed local source and immutable per-file snapshots. Each candidate's relevance is a separate Noul; all families share candidate and evidence budgets. The final assessment sees the original bound question and additional source, not previous verdicts or routing/relevance scores.

`core/selection.py` is shared by preparation and enrichment; `auxiliary_questions` derives meaning from the active YAML question and exact target, including custom criteria. No code branches on JEV01–JEV09.

Report reviews record disposition, all family probabilities, admitted families, per-family coverage, candidate memberships, selected source, omissions, and stopping outcomes. The resulting `retrieval_coverage` contains `families`, `candidate_families`, and combined-pool omission counts. Family membership is lexical/provenance information, not a resolved contract.

Configuration version 4 is additive YAML. Rule names (JEV01–JEV09 for built-ins) are stable identifiers. Titles/rulesets are report metadata, not model instructions. Disabling a rule or set prevents creating those questions; selection may also change request batching/cache identities.

Source outside the scanned subdirectory can be selected only under the resolved project root and filters. Selected evidence does not imply all callers have been seen or that per-file snapshots form an atomic repository revision. See [ENRICHMENT.md](ENRICHMENT.md) for the precise algorithm, research basis, and live-acceptance boundary.
