"""Generate bounded reporting-policy candidates from observed calibration material.

This module owns threshold-space construction only. It does not score candidate
policies or decide which policy should be selected.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Mapping

from jevscan.core.calibration_cases import CalibrationCase
from jevscan.core.protocol import ChoiceAnswer, NoulAnswer, ScoreAnswer, encode
from jevscan.core.rules import ReportPolicy, Rule, ScoreQuestion


@dataclass(frozen=True, slots=True)
class _ThresholdDimension:
    name: str
    target: tuple[str, str]
    values: tuple[Any, ...]


def _policy_document(policy: ReportPolicy) -> dict[str, Any]:
    return policy.model_dump(mode="json")


def policy_hash(policy: ReportPolicy) -> str:
    """Return the stable hash written into selection audits."""
    return f"sha256:{hashlib.sha256(encode(_policy_document(policy))).hexdigest()}"


def _answer_probability(case: CalibrationCase, policy: ReportPolicy, levels: Iterable[int] | None = None) -> float:
    answer = case.answer
    if isinstance(answer, NoulAnswer):
        return answer.noul if policy.expected else 1 - answer.noul
    if isinstance(answer, ChoiceAnswer):
        if not policy.choices:
            return 0.0
        return answer.probabilities[answer.choice] if answer.choice in policy.choices else 0.0
    assert isinstance(answer, ScoreAnswer)
    selected = list(levels or ())
    if not selected:
        return 0.0
    return sum(answer.probabilities[str(level)] for level in selected)


def _observed_values(values: Iterable[float], baseline: float | None = None) -> list[float]:
    result = {0.0, 1.0}
    result.update(float(value) for value in values)
    if baseline is not None:
        result.add(float(baseline))
    return sorted(result)


def _ordered_subsets(values: tuple[int, ...]) -> list[tuple[int, ...]]:
    """Return contiguous subsets of explicitly declared ordered levels."""
    return [values[start:end] for start in range(len(values)) for end in range(start + 1, len(values) + 1)]


def _centered_values(values: Iterable[Any], baseline: Any) -> tuple[Any, ...]:
    unique = list(dict.fromkeys(values))
    if isinstance(baseline, tuple):
        key = lambda value: (len(set(value) ^ set(baseline)), value)
    else:
        key = lambda value: (abs(float(value) - float(baseline)), value)
    return tuple(sorted(unique, key=key))


def _add_dimension(
    dimensions: list[_ThresholdDimension],
    warning: dict[str, Any],
    error: dict[str, Any],
    name: str,
    severity: str,
    field: str,
    values: Iterable[Any],
) -> None:
    baseline = warning[field] if severity == "warning" else error[field]
    if field == "score_levels" and baseline is not None:
        baseline = tuple(baseline)
    if baseline is None:
        return
    options = _centered_values(values, baseline)
    if len(options) > 1:
        dimensions.append(_ThresholdDimension(name, (severity, field), options))


def _score_dimension_values(
    rule: Rule, material: list[CalibrationCase], warning: dict[str, Any], error: dict[str, Any]
) -> set[float]:
    if not isinstance(rule.question, ScoreQuestion):
        return set()
    field = "min_score" if warning["min_score"] is not None else "max_score"
    max_level = len(rule.question.criteria) - 1
    values = {float(level) for level in range(max_level + 1)}
    values.update(float(case.answer.score) for case in material if isinstance(case.answer, ScoreAnswer))
    values = {value for value in values if 0 <= value <= max_level}
    values.update(value for value in (warning[field], error[field]) if value is not None)
    return values


def _candidate_dimensions(rule: Rule, material: list[CalibrationCase]) -> list[_ThresholdDimension]:
    report = rule.report
    warning = report.levels.warning.model_dump(mode="python")
    error = report.levels.error.model_dump(mode="python")
    dimensions: list[_ThresholdDimension] = []
    if warning["min_probability"] is not None:
        if isinstance(rule.question, ScoreQuestion) and warning["score_levels"] is not None:
            probabilities = [_answer_probability(case, report, warning["score_levels"]) for case in material] + [
                _answer_probability(case, report, error["score_levels"]) for case in material
            ]
        else:
            probabilities = [_answer_probability(case, report) for case in material]
        probability_values = _observed_values(probabilities, warning["min_probability"])
        probability_values = _observed_values(probability_values, error["min_probability"])
        _add_dimension(
            dimensions, warning, error, "warning_probability", "warning", "min_probability", probability_values
        )

    observed_confidence = _observed_values(
        case.answer.confidence for case in material if isinstance(case.answer, (ChoiceAnswer, ScoreAnswer))
    )
    if warning["min_confidence"] is not None:
        confidence_values = _observed_values(observed_confidence, warning["min_confidence"])
        confidence_values = _observed_values(confidence_values, error["min_confidence"])
        _add_dimension(dimensions, warning, error, "warning_confidence", "warning", "min_confidence", confidence_values)

    if isinstance(rule.question, ScoreQuestion) and warning["score_levels"] is None:
        field = "min_score" if warning["min_score"] is not None else "max_score"
        _add_dimension(
            dimensions,
            warning,
            error,
            "warning_score",
            "warning",
            field,
            _score_dimension_values(rule, material, warning, error),
        )

    if isinstance(rule.question, ScoreQuestion) and warning["score_levels"] is not None:
        _add_dimension(
            dimensions,
            warning,
            error,
            "warning_mass",
            "warning",
            "score_levels",
            _ordered_subsets(tuple(warning["score_levels"])),
        )
    return dimensions


def _policy_from_changes(rule: Rule, changes: Mapping[tuple[str, str], Any]) -> ReportPolicy | None:
    report = rule.report
    warning, error = report.levels.warning.model_dump(mode="python"), report.levels.error.model_dump(mode="python")
    for (severity, field), value in changes.items():
        target = warning if severity == "warning" else error
        target[field] = list(value) if field == "score_levels" else value
    try:
        candidate = ReportPolicy.model_validate({
            **report.model_dump(mode="python"),
            "levels": {"warning": warning, "error": error},
        })
        Rule.model_validate({**rule.model_dump(mode="python"), "report": candidate.model_dump(mode="python")})
    except ValueError:
        return None
    return candidate


def _interleave(streams: list[Iterator[ReportPolicy]]) -> Iterator[ReportPolicy]:
    active = list(streams)
    while active:
        next_active: list[Iterator[ReportPolicy]] = []
        for stream in active:
            try:
                yield next(stream)
            except StopIteration:
                continue
            next_active.append(stream)
        active = next_active


def _dimension_stream(
    dimensions: list[_ThresholdDimension],
    indices: tuple[int, ...],
    rule: Rule,
) -> Iterator[ReportPolicy]:
    if len(indices) == 1:
        dimension = dimensions[indices[0]]
        for value in dimension.values:
            policy = _policy_from_changes(rule, {dimension.target: value})
            if policy is not None:
                yield policy
        return
    first, second = (dimensions[index] for index in indices)
    for left in first.values:
        for right in second.values:
            policy = _policy_from_changes(rule, {first.target: left, second.target: right})
            if policy is not None:
                yield policy


@dataclass(frozen=True, slots=True)
class _SearchBudget:
    limit: int
    coordinate: int
    pair: int
    pairs: tuple[tuple[int, int], ...]
    structurally_truncated: bool

    @classmethod
    def for_dimensions(cls, dimensions: list[_ThresholdDimension], max_candidates: int) -> "_SearchBudget":
        limit = max(1, max_candidates)
        pairs = tuple((index, other) for index in range(len(dimensions)) for other in range(index + 1, len(dimensions)))
        coordinate = 1 if limit == 1 else max(2, limit // max(1, 2 * len(dimensions)))
        pair = 1 if limit == 1 else max(2, limit // max(1, 2 * len(pairs)))
        truncated = (
            any(len(dimension.values) > coordinate for dimension in dimensions)
            or any(len(dimensions[left].values) * len(dimensions[right].values) > pair for left, right in pairs)
            or len(dimensions) > 2
        )
        return cls(limit, coordinate, pair, pairs, truncated)


def _collect_candidates(
    unique: dict[str, ReportPolicy],
    streams: Iterable[Iterator[ReportPolicy]],
    limit: int,
) -> bool:
    """Add interleaved unique candidates; return whether the global limit was reached."""
    for policy in _interleave(list(streams)):
        candidate_hash = policy_hash(policy)
        if candidate_hash in unique:
            continue
        if len(unique) >= limit:
            return True
        unique[candidate_hash] = policy
        if len(unique) >= limit:
            return True
    return False


def _budgeted_candidates(
    rule: Rule,
    dimensions: list[_ThresholdDimension],
    max_candidates: int,
) -> tuple[tuple[ReportPolicy, ...], int, bool]:
    budget = _SearchBudget.for_dimensions(dimensions, max_candidates)
    unique: dict[str, ReportPolicy] = {policy_hash(rule.report): rule.report}

    coordinate_streams = (
        _limited_stream(_dimension_stream(dimensions, (index,), rule), budget.coordinate)
        for index in range(len(dimensions))
    )
    if _collect_candidates(unique, coordinate_streams, budget.limit):
        return tuple(unique.values()), len(unique), True

    pair_streams = (
        _limited_stream(_dimension_stream(dimensions, indices, rule), budget.pair) for indices in budget.pairs
    )
    reached_limit = _collect_candidates(unique, pair_streams, budget.limit)
    return (
        tuple(unique.values()),
        len(unique),
        reached_limit or budget.structurally_truncated,
    )


def candidate_policies(
    rule: Rule,
    cases: Iterable[CalibrationCase],
    *,
    max_candidates: int = 4096,
) -> tuple[ReportPolicy, ...]:
    """Generate bounded, valid threshold variants for one rule.

    Score mass candidates are restricted to non-empty subsets of the levels
    already declared by the baseline policy.  This is deliberately not a
    universal ``high score means bad`` assumption.
    """

    material = sorted(cases, key=lambda case: (case.case_id, case.split))
    policies, _, _ = candidate_policies_with_metadata(
        rule,
        material,
        max_candidates=max_candidates,
    )
    return policies


def _limited_stream(stream: Iterator[ReportPolicy], limit: int) -> Iterator[ReportPolicy]:
    for index, policy in enumerate(stream):
        if index >= limit:
            return
        yield policy


def candidate_policies_with_metadata(
    rule: Rule,
    material: list[CalibrationCase],
    *,
    max_candidates: int,
) -> tuple[tuple[ReportPolicy, ...], int, bool]:
    """Generate a balanced, bounded baseline/coordinate/pairwise family."""
    dimensions = _candidate_dimensions(rule, material)
    return _budgeted_candidates(rule, dimensions, max_candidates)
