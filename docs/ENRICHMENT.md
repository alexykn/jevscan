# Bounded evidence enrichment

## Purpose and limits

Enrichment addresses missing source evidence, not every form of model uncertainty. A low-confidence judgment can have adequate context; more source does not necessarily improve it. The scanner owns selection, filesystem access, budgets, snapshots, caching, and stopping. Jev supplies typed evidence judgments, not generated paths or executable tool calls.

The control flow remains one bounded pass:

```text
initial answer
  -> actionable uncertainty: disposition Choice + independent family Nouls
       -> stop / not applicable / disposition uncertain / no qualifying family
       -> one or more qualifying families
            -> deterministic local candidate discovery
            -> bounded deduplicated pool
            -> independent candidate relevance judgments
            -> append complete fitting evidence
            -> original question reassessed once, then stop
```

Module/tree scoring, persistent model sessions, compiler-resolved call graphs, and arbitrary repository agents are not implemented.

## Admission and priority

`review_trigger` uses the validated rule's `enrich_on` and assessment reason. It admits unknown checks only. Missing evidence precedes reduced context, applicability, explicit confidence/Choice ambiguity opt-ins, then explicit Noul ambiguity opt-ins. Reduced context can qualify even when low confidence is the primary displayed reason. File-local scheduling builds the priority queue before consuming the shared review budget, with deterministic source-position/rule-name ties. Source-order presentation is unaffected.

The packaged default is missing/reduced evidence, plus applicability for JEV06. Ordinary low confidence/ambiguous Nouls do not invoke the router unless explicitly enabled. A possible not-applicable result is not a clean judgment. Above-threshold uncertain warnings/errors remain visible without verbose mode regardless of routing admission.

For the packaged context-sensitive Choice rules, `insufficient_context` is deliberately narrow. JEV04 may use it only after a concrete repeated validation is visible; JEV05 only after a concrete fallback, swallowed failure, or best-effort continuation is visible; and JEV06 only after helper decomposition is visibly present. If the phenomenon itself is absent, the rule must choose its clean/not-applicable outcome instead of requesting hypothetical external context. This keeps enrichment focused on an identified construct whose classification genuinely depends on a missing fact.

## Disposition and independent evidence families

The disposition Choice answers a single control-flow decision:

- `not_applicable`: the target does not contain the operation the rule evaluates;
- `sufficient`: the rule applies and current evidence is adequate;
- `local_evidence`: additional local source could establish a concrete missing fact;
- `unavailable`: the required fact is external/runtime-only and unlikely to be established locally.

In `targeted` mode, each rule chooses from four independent evidence families: callers, callees (syntax-linked referenced definitions), tests, and enclosing owner/file context. Internally the callee family uses the existing `definitions` retrieval route. `full` exposes all families; `off` performs no enrichment. Each family Noul uses an explicit speculative premise: **assuming the rule applies and local source could help**. The questions cannot see each other's answers. A family score is not a normalized share of one distribution; several families or none may qualify.

The disposition Choice and the admitted family questions normally share one request. If configured token/byte/question budgets require it, they split into bounded batches using the same state. Every batch consumes the existing auxiliary-call budget, including cache hits. Partial routing answers remain auditable but never become an incomplete routing decision. The full routing result is required before retrieval.

The disposition must pass its configured probability/confidence gates. Only `local_evidence` admits families above `min_evidence_probability`. A terminal or uncertain disposition stops even when speculative family scores are high. A confident local disposition with no qualifying families stops as `no_evidence_family`. These explicit stops are not proof that retrieval would never help; they are the bounded policy decisions made from these predictions.

## Discovery and pooling

`SourceIndex` retains its existing syntax/name-based discovery. A file-scoped source snapshot supplies possible call sites, referenced definitions, test references, and enclosing source. Aliases, dynamic dispatch, macros, runtime-generated behavior, external contracts, and cross-language relationships may be missed. Candidate labels never claim compiler-level resolution.

For each qualifying family, discovery applies the same source/root/include/exclude/Git-ignore rules and local limits. Family candidates are combined by deterministic round-robin, deduplicated by target plus snapshot identity, and capped by **one global `max_candidates`**. Thus one large caller pool does not automatically exclude a definition pool. The audit records all discovered family memberships for duplicate candidates, per-family omission counts, and combined-pool omissions. It does not claim the bounded preview pool includes all repository candidates.

The shared lazy catalogue is capped at 1,000 source files and 16 MiB by default. An invocation can retrieve source outside its scanned subdirectory, but never intentionally outside its resolved project root and filters. Reads reject symlinks at every path component, nonregular files, and observed size/mtime changes during reading. This is not a sandbox against all hostile filesystem races. Snapshots are per-file, not an atomic repository snapshot. A preview and its eventual complete evidence use the same source revision.

## Relevance and reassessment

Each candidate gets an independent Noul about whether its complete source could provide a concrete missing fact for the original question and exact target. It is not asked to support a positive verdict. Matching short names, already-present source, and unrelated tests are explicitly insufficient.

Relevant candidates are ranked, then admitted within `max_evidence` and the shared request budgets. Source is complete at the candidate target level; the original target/evidence is preserved. Overlapping UTF-8 spans are merged. Candidate previews may be bounded and say so; actual target source is never truncated.

The final request uses the **unchanged original question** and target, with enriched documents, relationships, source hashes, and coverage. It excludes the initial verdict, disposition prediction, family probabilities, relevance scores, and an expected conclusion. Selection changes evidence, not the rule. The answer can become clean, tentative, confirmed, not applicable, or remain uncertain. There is only one reassessment.

Every phase uses the shared inference/cache/transport validator and rate limiter. Provider size rejection or a local limit stops enrichment explicitly. Malformed answers, genuine service failures, and cancellation retain audits and follow normal incomplete-scan handling; they are not disguised as clean results.

## Audit and accounting

Report schema 8 records initial answer/cache/evidence provenance, scheduling trigger, uncertainty reason, each prediction's model/cache flag/request hash/raw answers, disposition, all family probabilities, admitted families, per-family retrieval coverage, candidate family membership/relevance, selected spans, omissions, and stop outcome.

The `enrichment_calls` counter counts prediction requests, not individual questions: five routing questions can be one call; a constrained request budget can split them. `enrichment_reviewed` counts admitted checks. `enrichment_reruns` counts actual final reassessments. `enrichment_resolved` counts previously unknown checks whose final status becomes conclusive, including applicability-only decisions. Findings count once, not once per inference phase.

Request-cache keys include the canonical request bytes. rc7 also stores each validated judgment independently under endpoint, model, exact evidence, exact bound question, and prompt compatibility. Changing candidate source invalidates affected judgments; changing only the HTTP batch around an otherwise identical judgment does not. Project selection/title/threshold changes do not themselves become model instructions.

## Research basis and acceptance

Reviewed on 2026-09-18:

- [Official TypeSafe agent guidance](https://github.com/typesafe-ai/skills/blob/65a39f393687675ce170e6094757de20370365b9/skills/typesafe-ai/SKILL.md): independent shared-state questions, speculative premises, Noul for multiple simultaneous labels, and fresh calls when evidence must be acquired. It also distinguishes distribution confidence from workflow correctness.
- [Official Python SDK](https://github.com/typesafe-ai/typesafe-sdk-python): typed `system_one(state=..., questions=...)` interface. This architecture does not assume a persistent provider conversation.
- The live documentation index, Noul and fan-out Markdown pages at `docs.typesafe.ai` were attempted but inaccessible in this environment. The accessible official guidance supports this decomposition; it does not establish measured code-review accuracy or validate our numerical gates.

RC3's displayed routing failures did not include full distributions. The explanation that callers/definitions/tests were competing alternatives was a **design hypothesis**, not an observed probability distribution. RC4 removes that inappropriate mutual exclusivity; it does not guarantee that the disposition will become confident or that retrieval will improve a judgment.

HTTP-boundary tests exercise multiple families, no families, conflicting speculative answers and terminal dispositions, deduplication, global budgets, request splitting, source restrictions, changed-caller cache identity, and unchanged final questions. These tests establish orchestration behavior, not model quality. No authenticated live call is part of this release's verification.

For acceptance, pin a model, use representative positive/negative examples requiring cross-file evidence, save JSONL with and without enrichment, and inspect the raw disposition/family/candidate results. Measure whether retrieved facts improve the final decisions, not just whether question marks disappear. Keep stop outcomes and added source available for review.


## Relationship to pre-answer context recovery

RC5 adds a distinct, bounded operation before or during the initial assessment: recover from configured or provider size limits using AST-local evidence. This is not triggered merely by a low-confidence answer. The full target is always preserved, including when a whole-file target must instead be omitted.

Both recovery and enrichment now use `selection.py` for candidate relevance questions, packing and response interpretation. Every question embeds the active YAML rule's complete instructions/criteria and exact target through `Check.auxiliary`; built-in IDs, custom IDs and renamed rulesets take the same path. The generic operation asks about usefulness as evidence, not about an expected defect. Neither workflow treats low relevance as proof of irrelevance.

Recovery is audited in `context_selection` and its auxiliary requests in `compaction_calls`; post-answer refinement stays in `reviews` / `enrichment_calls`. Source-rejection hints are per-file/invocation, not persistent estimates of a model's context limit. A recovered initial assessment may still legitimately need independent cross-file evidence. See [context recovery](CONTEXT_RECOVERY.md).

## RC7 targeted defaults

The built-in catalogue deliberately narrows evidence direction before model routing. JEV04 (redundant validation) may inspect callers and callees; JEV05 may additionally inspect tests; JEV06 (unhelpful decomposition) uses callees/enclosing context and does not search arbitrary callers. JEV01–JEV03 and JEV08 use callees/enclosing context. The complete-file JEV07 and JEV09 checks disable cross-source enrichment by default because their questions concern the supplied file itself.

This policy can be overridden per project. Broader retrieval may improve a custom rule, but it also increases candidate-selection and reassessment cost.
