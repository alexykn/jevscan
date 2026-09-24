# Follow-up semantic rule candidates

This file records candidates that remain worth investigating after the
JEV12–JEV16 promotion. They are not production rules and have no reserved JEV
numbers.

## Strong follow-up candidates

### Exposed mutable authority

**Problem:** An accessor returns a live object connected to an owner's internal
state, allowing callers to mutate that state or retain control over it without
an explicit ownership or lifetime arrangement.

**Current evidence:** Strong synthetic and held-out behavior, but the rule
missed the one source-supported example in the anonymous real-code review.

**Next step:** Improve how the question recognizes owner-connected state and
caller burden, then run a new frozen evaluation. Do not lower thresholds to
recover the existing miss.

### Lost information needed by callers

**Research name:** `NEW01_CONSUMER_CONFIRMED`

**Problem:** A transformation combines states that later code needs to
distinguish.

**Current evidence:** Strong synthetic and held-out results, but the rule missed
both source-supported examples in the anonymous real-code review.

**Next step:** Improve recognition of visible consumer requirements and rerun a
fresh evaluation. Names or similar field shapes must not establish the
producer/consumer relationship.

### Several outcomes hidden inside one primitive value

**Research name:** `IMPLICIT_OUTCOME_PARTITIONS`

**Problem:** An API uses a boolean, integer, null, or sentinel for several
meaningful outcomes, forcing callers to reconstruct what happened.

**Current evidence:** Promising development behavior. The frozen held-out run
found two of three positive groups, below the predeclared group-recall
requirement.

**Next step:** Clarify the boundary between a genuinely binary result, an
intentional transport encoding, and an API that hides several caller-relevant
outcomes.

### Invalid related values escaping together

**Research name:** `NEW02_INVALID_STATE_ESCAPE`

**Problem:** Code returns or publishes a combination of related values that
breaks a visible rule between them.

**Current evidence:** The revised question found two of three positive groups
and produced little noise, but did not meet the frozen group-recall gate.

**Next step:** Improve the evidence for the relationship between the values,
the owning boundary, and the caller-visible escape. Do not report an invalid
combination inferred only from names.

### Overlapping work on the same key without coordination

**Research name:** `NEW10_UNCOORDINATED_SHARED_OPERATION`

**Problem:** Two overlapping operations on the same logical key can both pass a
check and then duplicate work, overwrite each other, or repeat an external
effect.

**Current evidence:** Excellent synthetic results. The earlier question inferred
concurrency too readily in real code and produced unacceptable review noise.

**Next step:** Require visible proof of two distinct overlapping executions on
the same key, plus a concrete duplicate or lost-update consequence. A
read-modify-write shape, `async`, `await`, or a closure is not enough.

## Longer-shot redesign

### One operational policy scattered across separate controls

**Research name:** `SPLIT_OPERATIONAL_POLICY`

**Problem:** Several locations independently interpret pieces of one operational
decision, so one policy change requires coordinated edits.

**Current evidence:** The tested wording did not identify any positive group;
Jev treated the examples as ordinary explicit policy composition.

**Next step:** Redesign the question around a visible synchronized-change burden
that is not already represented by one policy object, table, resolver, or typed
configuration.

## Not currently prioritized

- Hidden ambient dependency: the tested form was noisy and overlaps JEV12.
- Mixed-version decision: the tested form had no useful positive recall.
- Incomplete aggregate exposure: the tested form had no useful positive recall.
- Caller-knowledge leakage as a separate rule: its useful part belongs with
  hidden outcome distinctions.
- Semantic projection fanout: the current rule schema and evidence did not
  support a stable actionable check.

Any future revision must use a new development selection, frozen held-out run,
and source-reviewed usefulness check. Existing failed or deferred results must
not be retuned into a pass.
