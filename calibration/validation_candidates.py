"""Bounded, repository-only JEV04 validation-pair extraction.

This module is an experiment, not part of the packaged scanner.  It finds
syntactically identical direct predicates in a requested target and returns
supplemental pair metadata for an existing whole-target review.  It does not
claim that the predicates establish the same invariant, and it never contacts
a provider.

The extractor is deliberately conservative:

* only a small, explicit set of Tree-sitter control/validation nodes is
  admitted for each supported grammar;
* raw UTF-8 predicate bytes are compared without semantic or textual
  normalization;
* adjacent occurrences are paired and all occurrence/group/pair limits are
  hard caps; and
* recovery, unsupported shapes, overlapping operations, and ambiguous
  ownership request the existing whole-target fallback instead of guessing.

The most important false positive is a repeated predicate whose value can
change between checks, for example after an await, callback, mutation, or
external call.  The extractor reports source spans and callable boundaries so
the model can inspect those facts; it does not infer whether a boundary
invalidates a guarantee.  A repeated expression can also be coincidental,
and a different spelling of an equivalent predicate is intentionally missed.
"""

from __future__ import annotations

import hashlib
from bisect import bisect_left
from dataclasses import dataclass
from enum import StrEnum
from itertools import pairwise
from typing import Any, Iterable, cast

from jevscan.core.models import CALLABLE_KINDS, ParsedFile, Target

SUPPORTED_GRAMMARS = frozenset({"python", "rust", "javascript", "typescript", "tsx", "perl"})

_OPERATIONS: dict[str, frozenset[str]] = {
    "python": frozenset({"if_statement", "elif_clause", "assert_statement"}),
    "rust": frozenset({"if_expression"}),
    "javascript": frozenset({"if_statement"}),
    "typescript": frozenset({"if_statement"}),
    "tsx": frozenset({"if_statement"}),
    "perl": frozenset({"conditional_statement", "elsif", "postfix_conditional_expression"}),
}

_UNSUPPORTED_OPERATIONS: dict[str, frozenset[str]] = {
    "python": frozenset({
        "while_statement",
        "for_statement",
        "match_statement",
        "case_clause",
        "conditional_expression",
    }),
    "rust": frozenset({
        "while_expression",
        "for_expression",
        "loop_expression",
        "match_expression",
        "match_arm",
        "let_condition",
    }),
    "javascript": frozenset({
        "while_statement",
        "for_statement",
        "for_in_statement",
        "do_statement",
        "switch_case",
        "ternary_expression",
        "catch_clause",
    }),
    "typescript": frozenset({
        "while_statement",
        "for_statement",
        "for_in_statement",
        "do_statement",
        "switch_case",
        "ternary_expression",
        "catch_clause",
    }),
    "tsx": frozenset({
        "while_statement",
        "for_statement",
        "for_in_statement",
        "do_statement",
        "switch_case",
        "ternary_expression",
        "catch_clause",
    }),
    "perl": frozenset({
        "loop_statement",
        "for_statement",
        "cstyle_for_statement",
        "try_statement",
        "conditional_expression",
        "unless_statement",
    }),
}

_CALL_NODES = frozenset({
    "call",
    "call_expression",
    "func1op_call_expression",
    "function_call_expression",
    "method_call_expression",
    "ambiguous_function_call_expression",
})
_VALIDATION_FACT_NODES = _CALL_NODES | frozenset({
    "assert_expression",
    "assert_statement",
    "binary_expression",
    "boolean_operator",
    "comparison_expression",
    "comparison_operator",
    "conditional_expression",
    "conditional_statement",
    "if_expression",
    "if_statement",
    "in_operator",
    "is_operator",
    "macro_invocation",
    "match_expression",
    "type_check",
    "unary_expression",
    "unless_statement",
})
_RUST_ASSERT_MACROS = frozenset({"assert", "debug_assert", "assert_eq", "assert_ne"})
_UNSUPPORTED_CONDITIONS = frozenset({"ERROR", "let_condition", "macro_invocation", "token_tree"})


class ExtractionKind(StrEnum):
    """The action the caller should take for the requested target."""

    CANDIDATES = "candidates"
    WHOLE_TARGET_FALLBACK = "whole_target_fallback"
    NOT_APPLICABLE = "not_applicable"


class FallbackReason(StrEnum):
    """Stable reasons that prevent a narrow candidate extraction."""

    UNSUPPORTED_GRAMMAR = "unsupported_grammar"
    PARSE_RECOVERY = "parse_recovery"
    UNSUPPORTED_VALIDATION_SHAPE = "unsupported_validation_shape"
    AMBIGUOUS_OWNERSHIP = "ambiguous_ownership"
    INVALID_TARGET = "invalid_target"
    OCCURRENCE_CAP = "occurrence_cap"
    GROUP_CAP = "group_cap"
    PAIR_CAP = "pair_cap"
    NO_EXACT_PREDICATE_GROUP = "no_exact_predicate_group"


@dataclass(frozen=True, slots=True)
class ExtractionLimits:
    """Hard limits for the bounded search.

    Limits reject the extraction rather than silently truncating evidence.
    ``max_pairs`` applies to adjacent pairs across all exact predicate groups.
    """

    max_occurrences: int = 256
    max_groups: int = 64
    max_pairs: int = 128


DEFAULT_LIMITS = ExtractionLimits()


@dataclass(frozen=True, slots=True)
class SourceSpan:
    """A 0-based half-open UTF-8 byte span with ContextBuilder line bounds."""

    start_byte: int
    end_byte: int
    start_line: int
    end_line: int

    def as_dict(self) -> dict[str, int]:
        return {
            "start_byte": self.start_byte,
            "end_byte": self.end_byte,
            "start_line": self.start_line,
            "end_line": self.end_line,
        }

    def __getitem__(self, key: str) -> int:
        return self.as_dict()[key]


@dataclass(frozen=True, slots=True)
class CallableBoundary:
    """The nearest callable owner and its nesting depth for one occurrence."""

    owner_unit_id: str | None
    owner_name: str | None
    owner_kind: str | None
    depth: int
    boundary_span: SourceSpan | None
    boundary_type: str

    def as_dict(self) -> dict[str, Any]:
        owner = (
            None
            if self.owner_unit_id is None
            else {
                "id": self.owner_unit_id,
                "name": self.owner_name,
                "kind": self.owner_kind,
            }
        )
        return {
            "owner_unit": owner,
            "owner_unit_id": self.owner_unit_id,
            "owner_name": self.owner_name,
            "owner_kind": self.owner_kind,
            "depth": self.depth,
            "boundary_span": self.boundary_span.as_dict() if self.boundary_span else None,
            "boundary_type": self.boundary_type,
            "type": self.boundary_type,
            # This is an annotation for the model, not a semantic judgment.
            "boundary_invalidates_guarantee": None,
        }

    @property
    def owner_id(self) -> str | None:
        """Short alias useful to callers comparing two occurrence owners."""
        return self.owner_unit_id


@dataclass(frozen=True, slots=True)
class ValidationOccurrence:
    """One admitted validation/control operation."""

    occurrence: int
    operation_type: str
    raw_predicate: str
    predicate_sha256: str
    expression_span: SourceSpan
    predicate_span: SourceSpan
    operation_span: SourceSpan
    callable_boundary: CallableBoundary
    source_path: str

    @property
    def occurrence_order(self) -> int:
        return self.occurrence

    def as_dict(self) -> dict[str, Any]:
        boundary = self.callable_boundary.as_dict()
        return {
            "occurrence": self.occurrence,
            "occurrence_order": self.occurrence,
            "operation_type": self.operation_type,
            "raw_predicate": self.raw_predicate,
            "predicate_sha256": self.predicate_sha256,
            "expression_span": self.expression_span.as_dict(),
            "predicate_span": self.predicate_span.as_dict(),
            "operation_span": self.operation_span.as_dict(),
            "validation_operation_span": self.operation_span.as_dict(),
            "callable_boundary": boundary,
            "source_path": self.source_path,
        }

    @property
    def metadata(self) -> dict[str, Any]:
        return self.as_dict()


@dataclass(frozen=True, slots=True)
class ValidationPair:
    """One adjacent earlier/later relation within an exact predicate group."""

    id: str
    group_id: str
    target_id: str
    source_path: str
    raw_predicate: str
    predicate_sha256: str
    earlier: ValidationOccurrence
    later: ValidationOccurrence
    intervening_span: SourceSpan
    intervening_bytes: str

    @property
    def pair_id(self) -> str:
        return self.id

    @property
    def gap_span(self) -> SourceSpan:
        return self.intervening_span

    def as_dict(self) -> dict[str, Any]:
        earlier_boundary = self.earlier.callable_boundary
        later_boundary = self.later.callable_boundary
        owner_changed = earlier_boundary.owner_id != later_boundary.owner_id
        return {
            "id": self.id,
            "pair_id": self.id,
            "group_id": self.group_id,
            "target_id": self.target_id,
            "source_path": self.source_path,
            "raw_predicate": self.raw_predicate,
            "predicate_sha256": self.predicate_sha256,
            "earlier": self.earlier.as_dict(),
            "later": self.later.as_dict(),
            "earlier_occurrence": self.earlier.occurrence,
            "later_occurrence": self.later.occurrence,
            "intervening_span": self.intervening_span.as_dict(),
            "gap_span": self.intervening_span.as_dict(),
            "intervening_bytes": self.intervening_bytes,
            "callable_boundary": {
                "crossed": owner_changed,
                "owner_changed": owner_changed,
                "earlier_owner": earlier_boundary.as_dict(),
                "later_owner": later_boundary.as_dict(),
                # The extractor annotates this fact and leaves its meaning open.
                "invalidates_guarantee": None,
                "interpretation": "boundary annotation only; semantic invalidation is not inferred",
            },
        }

    @property
    def metadata(self) -> dict[str, Any]:
        return self.as_dict()


@dataclass(frozen=True, slots=True)
class ValidationGroup:
    """An exact raw-predicate group and its bounded adjacent pairs."""

    id: str
    target_id: str
    source_path: str
    raw_predicate: str
    predicate_sha256: str
    occurrences: tuple[ValidationOccurrence, ...]
    pairs: tuple[ValidationPair, ...]

    @property
    def candidate_pairs(self) -> tuple[ValidationPair, ...]:
        return self.pairs

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "target_id": self.target_id,
            "source_path": self.source_path,
            "raw_predicate": self.raw_predicate,
            "predicate_sha256": self.predicate_sha256,
            "occurrences": [occurrence.as_dict() for occurrence in self.occurrences],
            "pairs": [pair.as_dict() for pair in self.pairs],
        }


@dataclass(frozen=True, slots=True)
class ExtractionOutcome:
    """Explicit extraction result; candidates never replace the requested target."""

    kind: ExtractionKind
    target_id: str
    source_path: str
    groups: tuple[ValidationGroup, ...] = ()
    fallback_reason: FallbackReason | None = None
    detail: str | None = None

    @property
    def candidates(self) -> tuple[ValidationPair, ...]:
        return tuple(pair for group in self.groups for pair in group.pairs)

    @property
    def pairs(self) -> tuple[ValidationPair, ...]:
        return self.candidates

    @property
    def is_fallback(self) -> bool:
        return self.kind == ExtractionKind.WHOLE_TARGET_FALLBACK

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "target_id": self.target_id,
            "source_path": self.source_path,
            "groups": [group.as_dict() for group in self.groups],
            "candidate_count": len(self.candidates),
            "fallback_reason": self.fallback_reason.value if self.fallback_reason else None,
            "detail": self.detail,
        }

    @property
    def metadata(self) -> dict[str, Any]:
        return self.as_dict()


@dataclass(frozen=True, slots=True)
class _Operation:
    node: Any
    predicate: Any
    operation_type: str


def _walk(node: Any) -> Iterable[Any]:
    pending = [node]
    while pending:
        current = pending.pop()
        yield current
        pending.extend(reversed(current.children))


def _contains_parse_error(root: Any) -> bool:
    return any(node.type == "ERROR" or node.is_missing for node in _walk(root))


def _target_contains(node: Any, target: Target) -> bool:
    return target.start_byte <= node.start_byte and node.end_byte <= target.end_byte


def _target_intersects(node: Any, target: Target) -> bool:
    return node.start_byte < target.end_byte and target.start_byte < node.end_byte


def _location(newlines: list[int], start: int, end: int) -> SourceSpan:
    return SourceSpan(
        start,
        end,
        bisect_left(newlines, start) + 1,
        bisect_left(newlines, max(start, end - 1)) + 1,
    )


def _condition(node: Any, language: str) -> Any | None:
    condition = node.child_by_field_name("condition")
    if condition is not None:
        return condition
    # Python assert_statement has no named field for its first expression.
    if language == "python" and node.type == "assert_statement":
        return node.named_children[0] if node.named_children else None
    # Perl conditional grammar versions expose the predicate as the first or
    # last named child rather than a field (elsif uses the same convention).
    if language == "perl" and node.type in {"conditional_statement", "elsif"}:
        return node.named_children[0] if node.named_children else None
    if language == "perl" and node.type == "postfix_conditional_expression":
        return node.named_children[-1] if node.named_children else None
    return None


def _rust_macro_name(node: Any, source: bytes) -> str | None:
    if node.type != "macro_invocation" or not node.named_children:
        return None
    name = node.named_children[0]
    if name.type not in {"identifier", "scoped_identifier"}:
        return None
    return source[name.start_byte : name.end_byte].decode("utf-8").rsplit("::", 1)[-1]


def _unsupported_shape(root: Any, target: Target, language: str, source: bytes) -> str | None:
    unsupported = _UNSUPPORTED_OPERATIONS.get(language, frozenset())
    for node in _walk(root):
        if not _target_intersects(node, target):
            continue
        if node.type in unsupported and _target_contains(node, target):
            return f"unsupported control/validation node {node.type!r}"
        if (
            language == "rust"
            and _target_contains(node, target)
            and _rust_macro_name(node, source) in _RUST_ASSERT_MACROS
        ):
            return f"opaque Rust assertion macro {_rust_macro_name(node, source)!r}"
    return None


def _operations(root: Any, target: Target, language: str) -> tuple[list[_Operation], str | None]:
    admitted = _OPERATIONS.get(language, frozenset())
    operations: list[_Operation] = []
    for node in _walk(root):
        if node.type not in admitted:
            continue
        if not _target_contains(node, target):
            if _target_intersects(node, target):
                return [], f"operation {node.type!r} crosses the requested target boundary"
            continue
        predicate = _condition(node, language)
        if predicate is None or predicate.type in _UNSUPPORTED_CONDITIONS or predicate.is_missing:
            return [], f"unsupported direct condition shape for {node.type!r}"
        if predicate.start_byte < target.start_byte or predicate.end_byte > target.end_byte:
            return [], f"condition for {node.type!r} crosses the requested target boundary"
        # A Rust token tree or macro invocation is opaque even when it happens
        # to have repeated source text; do not guess its semantic predicate.
        if language == "rust" and any(child.type in {"macro_invocation", "token_tree"} for child in _walk(predicate)):
            return [], "Rust macro/token-tree condition is opaque"
        operations.append(_Operation(node, predicate, node.type))
    operations.sort(key=lambda item: (item.node.start_byte, item.node.end_byte))
    return operations, None


def _overlap_detail(operations: list[_Operation]) -> str | None:
    previous: _Operation | None = None
    for operation in operations:
        if previous is not None and operation.node.start_byte < previous.node.end_byte:
            return (
                f"overlapping operation spans {previous.operation_type!r} and "
                f"{operation.operation_type!r} cannot provide an exact gap"
            )
        previous = operation
    return None


def _callable_boundary(
    operation: _Operation,
    parsed: ParsedFile,
    newlines: list[int],
) -> CallableBoundary | None:
    callables = [
        unit
        for unit in parsed.units
        if unit.kind in CALLABLE_KINDS
        and unit.has_body
        and unit.start_byte <= operation.node.start_byte
        and operation.node.end_byte <= unit.end_byte
    ]
    callables.sort(key=lambda unit: (unit.start_byte, -unit.end_byte))
    if not callables:
        return CallableBoundary(None, None, None, 0, None, "file")
    smallest_size = callables[-1].end_byte - callables[-1].start_byte
    smallest = [unit for unit in callables if unit.end_byte - unit.start_byte == smallest_size]
    if len(smallest) != 1:
        return None
    owner = smallest[0]
    # The largest enclosing callable is depth zero.  This remains meaningful
    # for a file target and for a nested callable target alike.
    depth = max(0, len(callables) - 1)
    return CallableBoundary(
        owner.id,
        owner.name,
        owner.kind.value,
        depth,
        _location(newlines, owner.start_byte, owner.end_byte),
        owner.kind.value,
    )


def _fallback(
    target: Target,
    parsed: ParsedFile,
    reason: FallbackReason,
    detail: str,
) -> ExtractionOutcome:
    return ExtractionOutcome(
        ExtractionKind.WHOLE_TARGET_FALLBACK,
        target.id,
        parsed.path,
        fallback_reason=reason,
        detail=detail,
    )


def _validate_request(parsed: ParsedFile, requested: Target, limits: ExtractionLimits) -> ExtractionOutcome | None:
    if parsed.language not in SUPPORTED_GRAMMARS:
        return _fallback(
            requested,
            parsed,
            FallbackReason.UNSUPPORTED_GRAMMAR,
            f"grammar {parsed.language!r} is outside the experimental allowlist",
        )
    if (
        requested.path != parsed.path
        or requested.start_byte < 0
        or requested.end_byte < requested.start_byte
        or requested.end_byte > len(parsed.source)
    ):
        return _fallback(
            requested,
            parsed,
            FallbackReason.INVALID_TARGET,
            "requested target does not identify a span in the parsed source",
        )
    if any(parsed.diagnostics) or parsed.failed:
        return _fallback(
            requested,
            parsed,
            FallbackReason.PARSE_RECOVERY,
            "parsed file carries a diagnostic or failed parse status",
        )
    if limits.max_occurrences < 2 or limits.max_groups < 1 or limits.max_pairs < 1:
        return _fallback(
            requested,
            parsed,
            FallbackReason.OCCURRENCE_CAP,
            "limits cannot admit a pair",
        )
    return None


def _parse_root(parsed: ParsedFile, requested: Target) -> tuple[Any | None, ExtractionOutcome | None]:
    try:
        from tree_sitter_language_pack import SupportedLanguage, get_parser

        root = get_parser(cast(SupportedLanguage, parsed.language)).parse(parsed.source).root_node
    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        return None, _fallback(requested, parsed, FallbackReason.PARSE_RECOVERY, f"Tree-sitter parse failed: {exc}")
    if _contains_parse_error(root):
        return None, _fallback(
            requested,
            parsed,
            FallbackReason.PARSE_RECOVERY,
            "Tree-sitter reported a recovery/error node",
        )
    return root, None


def _collect_operations(
    root: Any, requested: Target, parsed: ParsedFile, limits: ExtractionLimits
) -> tuple[list[_Operation], ExtractionOutcome | None]:
    shape_detail = _unsupported_shape(root, requested, parsed.language, parsed.source)
    if shape_detail is not None:
        return [], _fallback(requested, parsed, FallbackReason.UNSUPPORTED_VALIDATION_SHAPE, shape_detail)
    operations, operation_detail = _operations(root, requested, parsed.language)
    if operation_detail is not None:
        return [], _fallback(requested, parsed, FallbackReason.UNSUPPORTED_VALIDATION_SHAPE, operation_detail)
    overlap_detail = _overlap_detail(operations)
    if overlap_detail is not None:
        return [], _fallback(requested, parsed, FallbackReason.UNSUPPORTED_VALIDATION_SHAPE, overlap_detail)
    if len(operations) > limits.max_occurrences:
        return [], _fallback(
            requested,
            parsed,
            FallbackReason.OCCURRENCE_CAP,
            f"{len(operations)} eligible operations exceed max_occurrences={limits.max_occurrences}",
        )
    return operations, None


def _no_operation_outcome(root: Any, requested: Target, parsed: ParsedFile) -> ExtractionOutcome:
    has_validation_fact = any(
        node.type in _VALIDATION_FACT_NODES for node in _walk(root) if _target_contains(node, requested)
    )
    if has_validation_fact:
        return _fallback(
            requested,
            parsed,
            FallbackReason.NO_EXACT_PREDICATE_GROUP,
            "target has validation syntax but no repeated admitted direct predicate",
        )
    return ExtractionOutcome(ExtractionKind.NOT_APPLICABLE, requested.id, parsed.path)


def _occurrence_data(
    operations: list[_Operation], parsed: ParsedFile, requested: Target, newlines: list[int]
) -> tuple[list[tuple[_Operation, CallableBoundary]], ExtractionOutcome | None]:
    occurrence_data: list[tuple[_Operation, CallableBoundary]] = []
    for operation in operations:
        boundary = _callable_boundary(operation, parsed, newlines)
        if boundary is None:
            return [], _fallback(
                requested,
                parsed,
                FallbackReason.AMBIGUOUS_OWNERSHIP,
                f"multiple equally small callable owners contain {operation.operation_type!r}",
            )
        occurrence_data.append((operation, boundary))
    return occurrence_data, None


def _exact_groups(
    occurrence_data: list[tuple[_Operation, CallableBoundary]],
    parsed: ParsedFile,
    requested: Target,
) -> tuple[list[tuple[bytes, list[tuple[_Operation, CallableBoundary]]]], ExtractionOutcome | None]:
    grouped: dict[bytes, list[tuple[_Operation, CallableBoundary]]] = {}
    for operation, boundary in occurrence_data:
        raw = parsed.source[operation.predicate.start_byte : operation.predicate.end_byte]
        grouped.setdefault(raw, []).append((operation, boundary))
    exact_groups = sorted(
        ((raw, values) for raw, values in grouped.items() if len(values) >= 2),
        key=lambda item: item[1][0][0].node.start_byte,
    )
    if not exact_groups:
        return [], _fallback(
            requested,
            parsed,
            FallbackReason.NO_EXACT_PREDICATE_GROUP,
            "no two admitted operations contain byte-identical direct predicates",
        )
    return exact_groups, None


def _build_pair(
    group_id: str,
    digest: str,
    raw: bytes,
    earlier: ValidationOccurrence,
    later: ValidationOccurrence,
    parsed: ParsedFile,
    requested: Target,
    newlines: list[int],
) -> ValidationPair:
    gap_start = earlier.operation_span.end_byte
    gap_end = later.operation_span.start_byte
    pair_id = (
        f"{group_id}:pair:"
        f"{earlier.operation_span.start_byte}-{earlier.operation_span.end_byte}:"
        f"{later.operation_span.start_byte}-{later.operation_span.end_byte}"
    )
    return ValidationPair(
        pair_id,
        group_id,
        requested.id,
        parsed.path,
        raw.decode("utf-8"),
        digest,
        earlier,
        later,
        _location(newlines, gap_start, gap_end),
        parsed.source[gap_start:gap_end].decode("utf-8"),
    )


def _build_groups(
    exact_groups: list[tuple[bytes, list[tuple[_Operation, CallableBoundary]]]],
    parsed: ParsedFile,
    requested: Target,
    newlines: list[int],
) -> tuple[ValidationGroup, ...]:
    groups: list[ValidationGroup] = []
    for raw, values in exact_groups:
        digest = hashlib.sha256(raw).hexdigest()
        group_id = f"{requested.id}:predicate:{digest}"
        occurrences = tuple(
            ValidationOccurrence(
                group_index,
                operation.operation_type,
                raw.decode("utf-8"),
                digest,
                _location(newlines, operation.predicate.start_byte, operation.predicate.end_byte),
                _location(newlines, operation.predicate.start_byte, operation.predicate.end_byte),
                _location(newlines, operation.node.start_byte, operation.node.end_byte),
                boundary,
                parsed.path,
            )
            for group_index, (operation, boundary) in enumerate(values)
        )
        pairs = tuple(
            _build_pair(group_id, digest, raw, earlier, later, parsed, requested, newlines)
            for earlier, later in pairwise(occurrences)
        )
        groups.append(
            ValidationGroup(
                group_id,
                requested.id,
                parsed.path,
                raw.decode("utf-8"),
                digest,
                occurrences,
                pairs,
            )
        )
    return tuple(groups)


def extract_validation_candidates(
    parsed: ParsedFile,
    target: Target | None = None,
    *,
    limits: ExtractionLimits = DEFAULT_LIMITS,
) -> ExtractionOutcome:
    """Extract bounded exact-predicate groups for ``target``.

    ``target`` is the original requested :class:`~jevscan.core.models.Target`.
    Candidate pairs are supplemental metadata and never become replacement
    targets.  Omitting it extracts against the file target.
    """

    requested = target or Target.from_file(parsed)
    validation_error = _validate_request(parsed, requested, limits)
    if validation_error is not None:
        return validation_error

    root, parse_error = _parse_root(parsed, requested)
    if parse_error is not None:
        return parse_error
    assert root is not None

    operations, operation_error = _collect_operations(root, requested, parsed, limits)
    if operation_error is not None:
        return operation_error
    if not operations:
        return _no_operation_outcome(root, requested, parsed)

    newlines = [index for index, byte in enumerate(parsed.source) if byte == 10]
    occurrence_data, ownership_error = _occurrence_data(operations, parsed, requested, newlines)
    if ownership_error is not None:
        return ownership_error

    exact_groups, group_error = _exact_groups(occurrence_data, parsed, requested)
    if group_error is not None:
        return group_error
    if len(exact_groups) > limits.max_groups:
        return _fallback(
            requested,
            parsed,
            FallbackReason.GROUP_CAP,
            f"{len(exact_groups)} exact predicate groups exceed max_groups={limits.max_groups}",
        )

    total_pairs = sum(len(values) - 1 for _, values in exact_groups)
    if total_pairs > limits.max_pairs:
        return _fallback(
            requested,
            parsed,
            FallbackReason.PAIR_CAP,
            f"{total_pairs} adjacent pairs exceed max_pairs={limits.max_pairs}",
        )

    return ExtractionOutcome(
        ExtractionKind.CANDIDATES,
        requested.id,
        parsed.path,
        _build_groups(exact_groups, parsed, requested, newlines),
    )


def extract_candidates(
    parsed: ParsedFile,
    target: Target | None = None,
    *,
    limits: ExtractionLimits = DEFAULT_LIMITS,
) -> ExtractionOutcome:
    """Compatibility alias for the experimental extractor."""

    return extract_validation_candidates(parsed, target, limits=limits)
