# Architecture and self-review boundaries

The guiding rule is to improve human understanding, not to minimize a scanner's
score. A warning starts an investigation; it does not justify changing calibrated
questions, hiding source, or splitting a coherent algorithm into forwarding helpers.

## Ownership

- `cli/calibrate.py` composes replay or selection. `core/rule_writeback.py` owns
  the supplied YAML snapshot, comment-preserving threshold edits, complete-config
  validation, temporary files, and atomic publication. Packaged rules never become
  selection inputs through the validation step.
- `core/parser.py` coordinates native parsing and normalizes lexical units.
  `syntax_symbols.py` owns language-specific bindings and declaration extents;
  `syntax_recovery.py` owns the narrowly admitted TypeScript type-only grammar
  errors; `syntax_facts.py` owns lexical references and syntactic admission facts;
  `lexical_ownership.py` owns the shared innermost-span assignment used by facts
  and retrieval. None of these modules decides whether source is architecturally
  defective.
- `core/discovery.py` owns lazy traversal and inherited ignore scopes. Its walk
  object owns directory descriptors, so cancellation and exhausted iterators have
  one cleanup path. Traversal remains iterative and bounded by directory depth.
- `core/source_snapshot.py` owns no-follow, directory-relative, bounded reads.
  `core/retrieval.py` owns indexing and candidate relationships, including coverage
  accounting for unreadable, oversized, or unparsable sources.
- `core/client.py` owns transport and retry execution. `AttemptLedger` makes
  paid-attempt reservation/accounting explicit and atomic; `RejectionTracker`
  owns repeated provider-rejection state. Cancellation still completes attempt
  accounting, and callers no longer need to infer hidden counter mutations from
  generic transport helpers.
- Calibration now has explicit layers rather than one selection/replay godfile:
  `calibration_cases.py` owns strict case/provenance contracts;
  `calibration_capture_validation.py` validates stored route provenance;
  `calibration_compatibility.py` owns model/prompt fit compatibility;
  `calibration_candidates.py` owns bounded threshold-space generation;
  `calibration_scoring.py` owns objective metrics and candidate ordering;
  `calibration_preparation.py` owns per-rule material preparation;
  `calibration_selection.py` is the orchestration façade; and
  `semantic_calibration.py` owns production-path replay and reports.
- `enrichment_routing.py` owns pure routing/admission policy shared by live
  enrichment and calibration validation. `enrichment.py` owns the evidence
  refinement workflow, while `evidence_merge.py` owns exact-span evidence union.
- `core/execution.py` owns bounded recovery. An attempted request returns its
  follow-up work; workers alone enqueue it. Compaction, final complete-target
  fallback, omission, and queue completion remain explicit stages.
- `core/scan_pipeline.py` owns producer/parser/evaluator queues and stage
  termination. `core/scanner.py` owns one invocation's external resources,
  cancellation boundary, progress lifetime, and final reporting. `scan_events.py`
  owns stage reporting and summary accounting. The CLI remains outside core.

## Preserved contracts

The refactor does not change packaged rules, thresholds, prompt registries,
request limits, model selection, report schemas, or cache identities. Parser
source spans, target identities, language coverage, syntax admission, and
TypeScript recovery exclusions remain unchanged. Recovery retains complete
targets, rejection deduplication, bounded rounds, shared-state repacking, and
full worker capacity after fan-out. Recent nonfatal reduced-context reporting
is retained; genuinely omitted checks still make a scan incomplete.

YAML write-back continues to reject aliases and symlink inputs, preserve comments
and file permissions, validate before replacement, reject concurrent content or
identity changes, and leave an unchanged policy byte-for-byte untouched. Temporary
files are now owned from creation so validation errors and interruption also
clean them up. This is not a new cross-process locking guarantee.

## Review decisions

The uncertain language-specific binding and TypeScript grammar predicates remain
explicit syntax checks. Their branching documents actual supported shapes; hiding
those shapes behind a generic traversal framework would not improve this code.
The innermost-reference sweeps also retain their interval stacks and ordering.

The calibration-route verifier remains an external-input validation boundary,
but its pure routing contract and capture validation now have separate owners.
It still checks supplied configuration, every bound wire and answer, and complete
decision coverage before accepting a stored not-applicable result. Calibration
selection semantics are unchanged.

The low-confidence assessment, compaction, threshold-search, syntax, enrichment,
recovery, and pipeline findings were each reviewed. Where they represented mixed
ownership or duplicated mechanics, the responsibility was separated. Where they
describe strict grammar-shape recognition, the explicit branches remain because
they make the admitted syntax visible rather than hiding it behind a generic
matcher.

The automated suite tests software contracts. A fresh live self-scan is a separate
check; neither passing tests nor changing function boundaries proves that every
Jevscan finding has disappeared. Keep reviewing any new finding on its merits.
