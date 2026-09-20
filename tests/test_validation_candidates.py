"""Focused tests for the offline bounded JEV04 candidate extractor."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from calibration.validation_candidates import (
    ExtractionKind,
    ExtractionLimits,
    FallbackReason,
    extract_validation_candidates,
)
from jevscan.core.languages import language_for
from jevscan.core.models import FileJob, Target
from jevscan.core.parser import parse_source

pytestmark = [pytest.mark.parser, pytest.mark.usefixtures("grammar_runtime")]

ROOT = Path(__file__).parents[1]
FOCUSED = ROOT / "examples" / "calibration" / "focused" / "sources"
EXPANDED = ROOT / "examples" / "calibration" / "expanded" / "sources"


def _parse(path: Path):
    spec = language_for(path)
    assert spec is not None
    parsed = parse_source(path.read_bytes(), FileJob(str(path), path.name, spec.grammar, spec.language))
    assert not parsed.failed, parsed.diagnostics
    return parsed


def _unit_target(parsed, name: str) -> Target:
    unit = next(unit for unit in parsed.units if unit.qualified_name == name or unit.name == name)
    return Target.from_unit(unit)


@pytest.mark.parametrize(
    ("filename", "target_name", "raw_predicate", "earlier_lines", "later_lines"),
    [
        ("acorn.py", "store_record", 'record["version"] < 1', (2, 3), (5, 6)),
        ("ash.py", "verify_item", "captured is None", (3, 4), (6, 7)),
        ("beech.py", "accept_value", "not isinstance(value, int)", (2, 3), (6, 7)),
        ("cedar_red.py", "write_record", 'record["version"] < 1', (2, 3), (5, 6)),
        ("elm_red.py", "apply_change", 'item["state"] != "ready"', (2, 3), (5, 6)),
        ("fir_red.py", "finish_cycle", 'record["state"] != "ready"', (2, 3), (5, 6)),
    ],
)
def test_all_focused_jev04_fixtures_have_exact_adjacent_pairs(
    filename: str,
    target_name: str,
    raw_predicate: str,
    earlier_lines: tuple[int, int],
    later_lines: tuple[int, int],
) -> None:
    path = FOCUSED / filename
    parsed = _parse(path)
    target = _unit_target(parsed, target_name)

    outcome = extract_validation_candidates(parsed, target)

    assert outcome.kind == ExtractionKind.CANDIDATES
    assert outcome.fallback_reason is None
    assert len(outcome.groups) == 1
    assert len(outcome.pairs) == 1
    group = outcome.groups[0]
    pair = outcome.pairs[0]
    digest = hashlib.sha256(raw_predicate.encode()).hexdigest()
    assert group.id == f"{target.id}:predicate:{digest}"
    assert pair.id.startswith(f"{group.id}:pair:")
    assert pair.raw_predicate == raw_predicate
    assert [occurrence.occurrence for occurrence in group.occurrences] == [0, 1]
    assert (pair.earlier.operation_span.start_line, pair.earlier.operation_span.end_line) == earlier_lines
    assert (pair.later.operation_span.start_line, pair.later.operation_span.end_line) == later_lines
    assert (
        pair.intervening_bytes
        == parsed.source[pair.earlier.operation_span.end_byte : pair.later.operation_span.start_byte].decode()
    )
    assert pair.intervening_span.as_dict() == {
        "start_byte": pair.earlier.operation_span.end_byte,
        "end_byte": pair.later.operation_span.start_byte,
        "start_line": pair.earlier.operation_span.end_line,
        "end_line": pair.later.operation_span.start_line,
    }
    assert pair.earlier.expression_span.as_dict() == pair.earlier.predicate_span.as_dict()
    assert pair.later.expression_span.as_dict() == pair.later.predicate_span.as_dict()


def test_quarry_and_summit_development_cases_keep_original_target_and_metadata() -> None:
    quarry = _parse(EXPANDED / "quarry.ts")
    quarry_target = _unit_target(quarry, "RevisionWriter.write")
    quarry_outcome = extract_validation_candidates(quarry, quarry_target)
    assert quarry_outcome.kind == ExtractionKind.CANDIDATES
    quarry_pair = quarry_outcome.pairs[0]
    assert quarry_pair.target_id == quarry_target.id
    assert quarry_pair.raw_predicate == "(record.version < 1)"
    assert (quarry_pair.earlier.operation_span.start_line, quarry_pair.earlier.operation_span.end_line) == (3, 5)
    assert (quarry_pair.later.operation_span.start_line, quarry_pair.later.operation_span.end_line) == (7, 9)

    summit = _parse(EXPANDED / "summit.js")
    outer = _unit_target(summit, "acceptParcel")
    outer_outcome = extract_validation_candidates(summit, outer)
    assert outer_outcome.kind == ExtractionKind.CANDIDATES
    summit_pair = outer_outcome.pairs[0]
    assert summit_pair.raw_predicate == "(!accepted)"
    assert summit_pair.earlier.callable_boundary.owner_name == "acceptParcel"
    assert summit_pair.later.callable_boundary.owner_kind == "closure"
    assert summit_pair.later.callable_boundary.depth == 2
    boundary = summit_pair.metadata["callable_boundary"]
    assert boundary["crossed"] is True
    assert boundary["invalidates_guarantee"] is None

    callback = next(
        unit
        for unit in summit.units
        if unit.kind.value == "closure" and unit.start_byte > outer.start_byte and unit.end_byte < outer.end_byte
    )
    callback_outcome = extract_validation_candidates(summit, Target.from_unit(callback))
    assert callback_outcome.kind == ExtractionKind.WHOLE_TARGET_FALLBACK
    assert callback_outcome.fallback_reason == FallbackReason.NO_EXACT_PREDICATE_GROUP
    assert not callback_outcome.pairs


def test_unicode_before_predicate_preserves_utf8_bytes_and_context_lines() -> None:
    source = (
        "# π before the target\n"
        "def check(value):\n"
        "    if value is None:\n"
        "        raise ValueError()\n"
        "    if value is None:\n"
        "        raise ValueError()\n"
    ).encode()
    path = Path("unicode.py")
    spec = language_for(path)
    assert spec is not None
    parsed = parse_source(source, FileJob("/tmp/unicode.py", path.name, spec.grammar, spec.language))
    target = _unit_target(parsed, "check")

    outcome = extract_validation_candidates(parsed, target)

    assert outcome.kind == ExtractionKind.CANDIDATES
    pair = outcome.pairs[0]
    first = source.index(b"value is None")
    second = source.index(b"value is None", first + 1)
    assert pair.earlier.expression_span.start_byte == first
    assert pair.later.expression_span.start_byte == second
    assert pair.earlier.expression_span.start_line == 3
    assert pair.later.expression_span.start_line == 5
    assert pair.earlier.expression_span.end_byte == first + len(b"value is None")
    assert pair.later.expression_span.end_byte == second + len(b"value is None")


def test_sibling_callbacks_are_pairable_but_each_callback_target_isolated() -> None:
    source = b"""function process(left, right) {
  const first = () => { if (left === null) throw Error("left"); };
  const second = () => { if (left === null) throw Error("right"); };
}
"""
    path = Path("siblings.js")
    spec = language_for(path)
    assert spec is not None
    parsed = parse_source(source, FileJob("/tmp/siblings.js", path.name, spec.grammar, spec.language))
    outer = _unit_target(parsed, "process")

    outer_outcome = extract_validation_candidates(parsed, outer)

    assert outer_outcome.kind == ExtractionKind.CANDIDATES
    pair = outer_outcome.pairs[0]
    assert pair.earlier.callable_boundary.owner_name == "first"
    assert pair.later.callable_boundary.owner_name == "second"
    assert pair.earlier.callable_boundary.depth == pair.later.callable_boundary.depth == 1
    assert pair.metadata["callable_boundary"]["crossed"] is True
    for unit in parsed.units:
        if unit.name in {"first", "second"}:
            isolated = extract_validation_candidates(parsed, Target.from_unit(unit))
            assert isolated.kind == ExtractionKind.WHOLE_TARGET_FALLBACK
            assert isolated.fallback_reason == FallbackReason.NO_EXACT_PREDICATE_GROUP


@pytest.mark.parametrize(
    ("language", "suffix", "source", "predicate"),
    [
        (
            "python",
            ".py",
            b"def f(value):\n    if is_valid(value):\n        return False\n    if is_valid(value):\n        return False\n",
            "is_valid(value)",
        ),
        (
            "rust",
            ".rs",
            b"fn f(value: Option<i32>) { if value.is_some() { return; } if value.is_some() { return; } }",
            "value.is_some()",
        ),
        (
            "javascript",
            ".js",
            b"function f(value) { if (!value) return; if (!value) return; }",
            "(!value)",
        ),
        (
            "typescript",
            ".ts",
            b"function f(value: { ready: boolean }) { if (value.ready) return; if (value.ready) return; }",
            "(value.ready)",
        ),
        (
            "perl",
            ".pl",
            b"sub f { if ($value) { return; } return if $value; }",
            "$value",
        ),
    ],
)
def test_representative_direct_condition_nodes_are_supported(
    language: str, suffix: str, source: bytes, predicate: str
) -> None:
    path = Path(f"condition{suffix}")
    spec = language_for(path)
    assert spec is not None and spec.language == language
    parsed = parse_source(source, FileJob(f"/tmp/{path.name}", path.name, spec.grammar, spec.language))

    outcome = extract_validation_candidates(parsed, Target.from_file(parsed))

    assert outcome.kind == ExtractionKind.CANDIDATES
    assert outcome.pairs[0].raw_predicate == predicate


def test_rust_assertion_macros_and_unsupported_control_shapes_fall_back() -> None:
    cases = [
        (
            "macro.rs",
            b"fn f(value: bool) { assert!(value); assert!(value); }",
            FallbackReason.UNSUPPORTED_VALIDATION_SHAPE,
        ),
        (
            "token_tree.rs",
            b"fn f(value: bool) { if check!(value) { return; } if check!(value) { return; } }",
            FallbackReason.UNSUPPORTED_VALIDATION_SHAPE,
        ),
        (
            "loop.py",
            b"def f(value):\n    while value:\n        value = False\n    while value:\n        value = False\n",
            FallbackReason.UNSUPPORTED_VALIDATION_SHAPE,
        ),
    ]
    for filename, source, reason in cases:
        path = Path(filename)
        spec = language_for(path)
        assert spec is not None
        parsed = parse_source(source, FileJob(f"/tmp/{filename}", filename, spec.grammar, spec.language))
        outcome = extract_validation_candidates(parsed, Target.from_file(parsed))
        assert outcome.kind == ExtractionKind.WHOLE_TARGET_FALLBACK
        assert outcome.fallback_reason == reason
        assert not outcome.pairs


def test_caps_and_no_exact_match_are_explicit_fallbacks() -> None:
    source = b"""def f(value):
    if value is None:
        return
    if value is None:
        return
"""
    path = Path("caps.py")
    spec = language_for(path)
    assert spec is not None
    parsed = parse_source(source, FileJob("/tmp/caps.py", path.name, spec.grammar, spec.language))
    target = Target.from_file(parsed)

    capped = extract_validation_candidates(parsed, target, limits=ExtractionLimits(max_occurrences=1))
    assert capped.kind == ExtractionKind.WHOLE_TARGET_FALLBACK
    assert capped.fallback_reason == FallbackReason.OCCURRENCE_CAP

    different = replace(parsed, source=source.replace(b"value is None", b"value is not None", 1))
    different_outcome = extract_validation_candidates(different, Target.from_file(different))
    assert different_outcome.kind == ExtractionKind.WHOLE_TARGET_FALLBACK
    assert different_outcome.fallback_reason == FallbackReason.NO_EXACT_PREDICATE_GROUP


def test_group_and_pair_caps_reject_truncation() -> None:
    source = b"""def f(value):
    if value is None:
        return
    if value is None:
        return
    if value is not None:
        return
    if value is not None:
        return
    if value is None:
        return
"""
    path = Path("many.py")
    spec = language_for(path)
    assert spec is not None
    parsed = parse_source(source, FileJob("/tmp/many.py", path.name, spec.grammar, spec.language))
    target = Target.from_file(parsed)

    group_capped = extract_validation_candidates(parsed, target, limits=ExtractionLimits(max_groups=1))
    assert group_capped.kind == ExtractionKind.WHOLE_TARGET_FALLBACK
    assert group_capped.fallback_reason == FallbackReason.GROUP_CAP

    pair_capped = extract_validation_candidates(
        parsed,
        target,
        limits=ExtractionLimits(max_groups=2, max_pairs=1),
    )
    assert pair_capped.kind == ExtractionKind.WHOLE_TARGET_FALLBACK
    assert pair_capped.fallback_reason == FallbackReason.PAIR_CAP


def test_unsupported_grammar_parse_recovery_ownership_and_no_fact_are_explicit() -> None:
    source = b"""def f(value):
    if value is None:
        return
    if value is None:
        return
"""
    path = Path("outcomes.py")
    spec = language_for(path)
    assert spec is not None
    parsed = parse_source(source, FileJob("/tmp/outcomes.py", path.name, spec.grammar, spec.language))
    target = Target.from_file(parsed)

    unsupported = extract_validation_candidates(replace(parsed, language="go"), target)
    assert unsupported.kind == ExtractionKind.WHOLE_TARGET_FALLBACK
    assert unsupported.fallback_reason == FallbackReason.UNSUPPORTED_GRAMMAR

    malformed = parse_source(
        b"def f(:\n    if value:\n        return\n", FileJob("/tmp/bad.py", "bad.py", "python", "python")
    )
    malformed_outcome = extract_validation_candidates(malformed, Target.from_file(malformed))
    assert malformed_outcome.kind == ExtractionKind.WHOLE_TARGET_FALLBACK
    assert malformed_outcome.fallback_reason == FallbackReason.PARSE_RECOVERY

    unit = parsed.units[0]
    ambiguous = replace(parsed, units=parsed.units + (replace(unit, id="duplicate:0:function"),))
    ambiguous_outcome = extract_validation_candidates(ambiguous, target)
    assert ambiguous_outcome.kind == ExtractionKind.WHOLE_TARGET_FALLBACK
    assert ambiguous_outcome.fallback_reason == FallbackReason.AMBIGUOUS_OWNERSHIP

    no_fact_source = b"def empty(value):\n    return value\n"
    no_fact = parse_source(
        no_fact_source,
        FileJob("/tmp/no_fact.py", "no_fact.py", "python", "python"),
    )
    no_fact_outcome = extract_validation_candidates(no_fact, Target.from_file(no_fact))
    assert no_fact_outcome.kind == ExtractionKind.NOT_APPLICABLE

    validation_without_pair = parse_source(
        b"def one_check(value):\n    return value == None\n",
        FileJob("/tmp/one_check.py", "one_check.py", "python", "python"),
    )
    validation_without_pair_outcome = extract_validation_candidates(
        validation_without_pair,
        Target.from_file(validation_without_pair),
    )
    assert validation_without_pair_outcome.kind == ExtractionKind.WHOLE_TARGET_FALLBACK
    assert validation_without_pair_outcome.fallback_reason == FallbackReason.NO_EXACT_PREDICATE_GROUP
