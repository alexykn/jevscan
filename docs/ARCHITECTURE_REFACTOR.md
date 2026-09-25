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
  errors; `syntax_facts.py` owns lexical references and syntactic admission facts.
  None of these modules decides whether source is architecturally defective.
- `core/discovery.py` owns lazy traversal and inherited ignore scopes. Its walk
  object owns directory descriptors, so cancellation and exhausted iterators have
  one cleanup path. Traversal remains iterative and bounded by directory depth.
- `core/source_snapshot.py` owns no-follow, directory-relative, bounded reads.
  `core/retrieval.py` owns indexing and candidate relationships, including coverage
  accounting for unreadable, oversized, or unparsable sources.
- `core/client.py` owns each admitted transport attempt. Request, token, and cost
  reservations commit together before transport; cancellation still completes
  attempt accounting. Transport retries, response retries, and provider rejections
  are separate operations, not separate policy implementations.
- `core/execution.py` owns bounded recovery. An attempted request returns its
  follow-up work; workers alone enqueue it. Compaction, final complete-target
  fallback, omission, and queue completion remain explicit stages.
- `core/scanner.py` owns worker queues and invocation resources. `scan_events.py`
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

The canonical calibration-route verifier remains an external-input validation
boundary: it must check supplied configuration, every bound wire and answer, and
complete decision coverage before accepting a stored not-applicable result. The
current PR does not relax that verifier or change calibration selection semantics.
Other uncertain assessment, enrichment, and threshold-search findings are not
blanket refactoring requirements.

The automated suite tests software contracts. A fresh live self-scan is a separate
check; neither passing tests nor changing function boundaries proves that every
Jevscan finding has disappeared. Keep reviewing any new finding on its merits.
