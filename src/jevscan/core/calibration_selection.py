"""Deterministic offline selection of reporting thresholds.

This module deliberately does not interpret answers itself.  Candidate policies
are evaluated by :func:`jevscan.core.semantic_calibration.replay_cases`, which
reuses the production ``Check`` and ``assess`` path.  The selector only
generates valid reporting-policy variants and scores their observed outcomes.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Mapping

from jevscan.core.protocol import ChoiceAnswer, NoulAnswer, ScoreAnswer, encode
from jevscan.core.rules import ReportPolicy, Rule, ScoreQuestion
from jevscan.core.semantic_calibration import CalibrationCase, CalibrationReport, ReplayRecord, replay_cases

SELECTION_VERSION = 1
_LABELS = ("Agree", "Partial", "Disagree")
_OUTCOMES = ("confirmed", "tentative", "none")
_DEFAULT_UTILITY = {
    "Agree": {"confirmed": 4.0, "tentative": 2.0, "none": -4.0},
    "Partial": {"confirmed": -1.0, "tentative": 1.0, "none": 0.0},
    "Disagree": {"confirmed": -8.0, "tentative": -2.0, "none": 0.0},
}


def _policy_document(policy: ReportPolicy) -> dict[str, Any]:
    return policy.model_dump(mode="json")


def policy_hash(policy: ReportPolicy) -> str:
    """Return the stable hash written into selection audits."""
    return f"sha256:{hashlib.sha256(encode(_policy_document(policy))).hexdigest()}"


@dataclass(frozen=True, slots=True)
class SelectionObjective:
    """Utility matrix and minimum independent-label support for selection.

    ``Agree`` is the positive label, ``Disagree`` is the negative label, and
    ``Partial`` is intentionally a separate row.  The defaults reward useful
    tentative findings, penalize missed positives, and make a false confirmed
    finding four times as costly as a false tentative finding.
    """

    utility: Mapping[str, Mapping[str, float]]
    min_positive_support: int = 1
    min_negative_support: int = 1
    max_candidates: int = 4096

    @classmethod
    def default(cls) -> SelectionObjective:
        return cls(
            utility={label: dict(values) for label, values in _DEFAULT_UTILITY.items()},
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> SelectionObjective:
        if value is None:
            return cls.default()
        document = dict(value)
        unknown = set(document) - {
            "utility",
            "labels",
            "min_positive_support",
            "min_negative_support",
            "max_candidates",
        }
        if unknown:
            raise ValueError(f"unknown selection objective fields: {', '.join(sorted(unknown))}")
        if "utility" in document and "labels" in document:
            raise ValueError("selection objective must use utility or labels, not both")
        utility_value = document.get("utility", document.get("labels"))
        if utility_value is None:
            utility_value = {label: dict(values) for label, values in _DEFAULT_UTILITY.items()}
        if not isinstance(utility_value, Mapping):
            raise TypeError("selection objective utility must be a mapping")
        unknown_labels = set(utility_value) - set(_LABELS)
        if unknown_labels:
            raise ValueError(f"unknown selection objective labels: {', '.join(sorted(unknown_labels))}")
        utility: dict[str, dict[str, float]] = {}
        for label in _LABELS:
            row = utility_value.get(label)
            if not isinstance(row, Mapping):
                raise TypeError(f"selection objective is missing {label} utility")
            unknown_outcomes = set(row) - set(_OUTCOMES)
            if unknown_outcomes:
                raise ValueError(f"unknown {label} utility outcomes: {', '.join(sorted(unknown_outcomes))}")
            utility[label] = {}
            for outcome in _OUTCOMES:
                raw = row.get(outcome)
                if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                    raise TypeError(f"selection objective {label}.{outcome} must be numeric")
                if not math.isfinite(float(raw)):
                    raise ValueError(f"selection objective {label}.{outcome} must be finite")
                utility[label][outcome] = float(raw)
        return cls(
            utility=utility,
            min_positive_support=_positive_int(document.get("min_positive_support", 1), "min_positive_support"),
            min_negative_support=_positive_int(document.get("min_negative_support", 1), "min_negative_support"),
            max_candidates=_positive_int(document.get("max_candidates", 4096), "max_candidates"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "utility": {label: dict(self.utility[label]) for label in _LABELS},
            "min_positive_support": self.min_positive_support,
            "min_negative_support": self.min_negative_support,
            "max_candidates": self.max_candidates,
        }


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"selection objective {name} must be a positive integer")
    return value


@dataclass(frozen=True, slots=True)
class CandidateMetrics:
    """Machine-readable metrics for one candidate policy."""

    policy: dict[str, Any]
    policy_hash: str
    objective: float
    records: int
    comparable_records: int
    label_counts: dict[str, int]
    outcome_counts: dict[str, dict[str, int]]
    support: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "policy": self.policy,
            "policy_hash": self.policy_hash,
            "objective": self.objective,
            "records": self.records,
            "comparable_records": self.comparable_records,
            "label_counts": self.label_counts,
            "outcome_counts": self.outcome_counts,
            "support": self.support,
        }


@dataclass(frozen=True, slots=True)
class _ThresholdDimension:
    name: str
    target: tuple[str, str]
    values: tuple[Any, ...]


@dataclass(frozen=True, slots=True)
class _PreparedRule:
    rule_id: str
    baseline: ReportPolicy
    baseline_rule: Rule
    development: list[CalibrationCase]
    heldout: list[CalibrationCase]
    mismatches: tuple[dict[str, Any], ...]
    development_mismatches: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class RuleSelection:
    rule_id: str
    selected_policy: ReportPolicy | None
    baseline_policy: ReportPolicy | None
    reason: str
    candidates: tuple[CandidateMetrics, ...]
    development: CandidateMetrics | None
    heldout: dict[str, Any] | None
    rule_mismatches: tuple[dict[str, Any], ...] = ()
    search: dict[str, Any] | None = None
    selected_development: CandidateMetrics | None = None

    def as_dict(self) -> dict[str, Any]:
        selected = _policy_document(self.selected_policy) if self.selected_policy is not None else None
        baseline = _policy_document(self.baseline_policy) if self.baseline_policy is not None else None
        return {
            "rule_id": self.rule_id,
            "reason": self.reason,
            "selected_policy": selected,
            "selected_policy_hash": policy_hash(self.selected_policy) if self.selected_policy else None,
            "baseline_policy": baseline,
            "baseline_policy_hash": policy_hash(self.baseline_policy) if self.baseline_policy else None,
            "development": self.development.as_dict() if self.development else None,
            "baseline_development": self.development.as_dict() if self.development else None,
            "selected_development": self.selected_development.as_dict() if self.selected_development else None,
            "candidates": [candidate.as_dict() for candidate in self.candidates],
            "heldout": self.heldout,
            "rule_mismatches": list(self.rule_mismatches),
            "search": self.search,
        }


@dataclass(frozen=True, slots=True)
class SelectionAudit:
    development_split: str
    heldout_split: str | None
    objective: SelectionObjective
    rules: dict[str, RuleSelection]

    @property
    def selected_policies(self) -> dict[str, ReportPolicy]:
        return {
            rule_id: result.selected_policy
            for rule_id, result in self.rules.items()
            if result.selected_policy is not None
        }

    def as_dict(self) -> dict[str, Any]:
        selected = {
            rule_id: {
                "policy": _policy_document(policy),
                "policy_hash": policy_hash(policy),
            }
            for rule_id, policy in sorted(self.selected_policies.items())
        }
        return {
            "version": SELECTION_VERSION,
            "development_split": self.development_split,
            "heldout_split": self.heldout_split,
            "objective": self.objective.as_dict(),
            "selected": selected,
            "rules": {rule_id: result.as_dict() for rule_id, result in sorted(self.rules.items())},
        }


def selected_policy_document(audit: SelectionAudit) -> dict[str, Any]:
    """Return the mapping accepted by the existing ``--report-policy`` loader."""
    return {
        "rules": {
            rule_id: policy.model_dump(mode="json", exclude_none=True)
            for rule_id, policy in sorted(audit.selected_policies.items())
        }
    }


def _support_key(case: CalibrationCase) -> str:
    """Return only an explicit, stable provenance grouping contract."""

    for name in ("source_group", "scenario_group", "owner_group"):
        value = case.provenance.get(name)
        if isinstance(value, str) and value.strip():
            return f"{name}:{value}"
    return ""


def _outcome(record: ReplayRecord) -> str:
    if record.confirmed:
        return "confirmed"
    if record.assessment.tentative_finding is not None:
        return "tentative"
    return "none"


def _candidate_metrics(
    policy: ReportPolicy,
    cases: list[CalibrationCase],
    objective: SelectionObjective,
    *,
    rule_id: str,
) -> CandidateMetrics:
    report = replay_cases(cases, {rule_id: policy})
    usable = [record for record in report.records if record.comparable]
    label_counts = {label: sum(record.case.label == label for record in usable) for label in _LABELS}
    outcome_counts = {
        label: {
            outcome: sum(record.case.label == label and _outcome(record) == outcome for record in usable)
            for outcome in _OUTCOMES
        }
        for label in _LABELS
    }
    group_sizes: dict[str, int] = defaultdict(int)
    for record in usable:
        group = _support_key(record.case)
        if group:
            group_sizes[group] += 1
    utility = sum(
        objective.utility[record.case.label][_outcome(record)] / group_sizes[_support_key(record.case)]
        for record in usable
        if record.case.label in objective.utility and _support_key(record.case)
    )
    groups_by_label: dict[str, set[str]] = defaultdict(set)
    missing_groups = 0
    for record in usable:
        group = _support_key(record.case)
        if group:
            groups_by_label[record.case.label].add(group)
        else:
            missing_groups += 1
    group_counts = {label: len(groups_by_label[label]) for label in _LABELS}
    return CandidateMetrics(
        policy=_policy_document(policy),
        policy_hash=policy_hash(policy),
        objective=utility,
        records=len(report.records),
        comparable_records=len(usable),
        label_counts=label_counts,
        outcome_counts=outcome_counts,
        support={
            "positive_groups": group_counts["Agree"],
            "negative_groups": group_counts["Disagree"],
            "partial_groups": group_counts["Partial"],
            "groups_by_label": group_counts,
            "missing_support_groups": missing_groups,
            "positive_signal": outcome_counts["Agree"]["confirmed"] + outcome_counts["Agree"]["tentative"],
            "non_comparable": len(report.records) - len(usable),
        },
    )


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


def _budgeted_candidates(
    rule: Rule,
    dimensions: list[_ThresholdDimension],
    max_candidates: int,
) -> tuple[tuple[ReportPolicy, ...], int, bool]:
    limit = max(1, max_candidates)
    coordinate_budget = 1 if limit == 1 else max(2, limit // max(1, 2 * len(dimensions)))
    pair_indices = [(index, other) for index in range(len(dimensions)) for other in range(index + 1, len(dimensions))]
    pair_budget = 1 if limit == 1 else max(2, limit // max(1, 2 * len(pair_indices)))
    unique: dict[str, ReportPolicy] = {policy_hash(rule.report): rule.report}
    coordinate_truncated = any(len(dimension.values) > coordinate_budget for dimension in dimensions)
    pair_truncated = any(
        len(dimensions[left].values) * len(dimensions[right].values) > pair_budget for left, right in pair_indices
    )

    streams = [
        _limited_stream(_dimension_stream(dimensions, (index,), rule), coordinate_budget)
        for index in range(len(dimensions))
    ]
    for policy in _interleave(streams):
        candidate_hash = policy_hash(policy)
        if candidate_hash in unique:
            continue
        if len(unique) >= limit:
            return tuple(unique.values()), len(unique), True
        unique[candidate_hash] = policy
        if len(unique) >= limit:
            return tuple(unique.values()), len(unique), True

    streams = [_limited_stream(_dimension_stream(dimensions, indices, rule), pair_budget) for indices in pair_indices]
    for policy in _interleave(streams):
        candidate_hash = policy_hash(policy)
        if candidate_hash in unique:
            continue
        if len(unique) >= limit:
            return tuple(unique.values()), len(unique), True
        unique[candidate_hash] = policy
        if len(unique) >= limit:
            return tuple(unique.values()), len(unique), True
    restricted = len(dimensions) > 2
    return tuple(unique.values()), len(unique), coordinate_truncated or pair_truncated or restricted


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
    policies, _, _ = _candidate_policies_with_metadata(
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


def _candidate_policies_with_metadata(
    rule: Rule,
    material: list[CalibrationCase],
    *,
    max_candidates: int,
) -> tuple[tuple[ReportPolicy, ...], int, bool]:
    """Generate a balanced, bounded baseline/coordinate/pairwise family."""
    dimensions = _candidate_dimensions(rule, material)
    return _budgeted_candidates(rule, dimensions, max_candidates)


def _policy_distance(left: Any, right: Any) -> int:
    def distance(first: Any, second: Any) -> int:
        if isinstance(first, Mapping) and isinstance(second, Mapping):
            return sum(distance(first.get(key), second.get(key)) for key in set(first) | set(second))
        if isinstance(first, list) and isinstance(second, list):
            shared = min(len(first), len(second))
            return sum(distance(first[index], second[index]) for index in range(shared)) + abs(len(first) - len(second))
        return int(first != second)

    return distance(_policy_document(left), _policy_document(right))


def _select_candidate(
    baseline: ReportPolicy,
    candidates: list[CandidateMetrics],
    *,
    require_positive_signal: bool,
) -> CandidateMetrics:
    baseline_hash = policy_hash(baseline)
    eligible = [
        candidate for candidate in candidates if not require_positive_signal or candidate.support["positive_signal"] > 0
    ]
    if not eligible:
        return next(candidate for candidate in candidates if candidate.policy_hash == baseline_hash)
    return max(
        eligible,
        key=lambda candidate: (
            candidate.objective,
            candidate.policy_hash == baseline_hash,
            -_policy_distance(
                ReportPolicy.model_validate(candidate.policy).levels.error,
                baseline.levels.error,
            ),
            -_policy_distance(
                ReportPolicy.model_validate(candidate.policy),
                baseline,
            ),
            _policy_sort_key(candidate.policy),
        ),
    )


def _policy_sort_key(policy: Mapping[str, Any]) -> str:
    return json.dumps(policy, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _semantic_contract(rule: Rule) -> dict[str, Any]:
    document = rule.model_dump(mode="json")
    report = document.pop("report")
    warning, error = report["levels"]["warning"], report["levels"]["error"]

    def threshold_mode(level: dict[str, Any]) -> dict[str, Any]:
        if level["score_levels"] is not None:
            return {"mode": "mass", "score_levels": level["score_levels"]}
        if level["min_score"] is not None:
            return {"mode": "min_score"}
        if level["max_score"] is not None:
            return {"mode": "max_score"}
        return {"mode": "probability"}

    document["report_semantics"] = {
        field: report[field]
        for field in ("choices", "expected", "uncertain_choices", "not_applicable_choices", "uncertain_range")
    }
    document["report_semantics"]["thresholds"] = {
        "warning": threshold_mode(warning),
        "error": threshold_mode(error),
    }
    return document


def _mismatch(authoritative: Rule, case: CalibrationCase) -> dict[str, Any] | None:
    expected = encode(_semantic_contract(authoritative))
    actual = encode(_semantic_contract(case.rule))
    if expected == actual:
        return None
    return {
        "case_id": case.case_id,
        "field": "rule_semantics",
        "expected_hash": f"sha256:{hashlib.sha256(expected).hexdigest()}",
        "actual_hash": f"sha256:{hashlib.sha256(actual).hexdigest()}",
    }


def _heldout_metrics(
    rule_id: str,
    policy: ReportPolicy,
    cases: list[CalibrationCase],
) -> dict[str, Any] | None:
    if not cases:
        return None
    report: CalibrationReport = replay_cases(cases, {rule_id: policy})
    return {
        "split": cases[0].split,
        "rules": {split: split_report.as_dict() for split, split_report in report.reports.get(rule_id, {}).items()},
        "non_comparable": list(report.non_comparable),
    }


def _not_searched_selection(
    rule_id: str,
    baseline: ReportPolicy | None,
    reason: str,
    mismatches: tuple[dict[str, Any], ...],
    limit: int,
    *,
    retain_baseline: bool,
) -> RuleSelection:
    return RuleSelection(
        rule_id,
        baseline if retain_baseline else None,
        baseline,
        reason,
        (),
        None,
        None,
        mismatches,
        {"limit": limit, "generated": 0, "truncated": False, "strategy": "not_searched"},
    )


def _reference_rule(
    development: list[CalibrationCase],
    authoritative: Rule | None,
) -> Rule | None:
    if authoritative is not None:
        return authoritative
    if not development:
        return None
    return min(development, key=lambda case: encode(_semantic_contract(case.rule))).rule


def _baseline_conflict(
    rule_id: str,
    development: list[CalibrationCase],
    limit: int,
) -> RuleSelection | None:
    fingerprints = {encode(case.rule.model_dump(mode="json")) for case in development}
    if len(fingerprints) <= 1:
        return None
    baseline_case = min(development, key=lambda case: encode(case.rule.model_dump(mode="json")))
    mismatches = tuple(
        {
            "case_id": case.case_id,
            "field": "development_baseline_policy",
            "expected_hash": f"sha256:{hashlib.sha256(encode(baseline_case.rule.report.model_dump(mode='json'))).hexdigest()}",
            "actual_hash": f"sha256:{hashlib.sha256(encode(case.rule.report.model_dump(mode='json'))).hexdigest()}",
        }
        for case in development
        if case.rule.report != baseline_case.rule.report
    )
    return _not_searched_selection(
        rule_id,
        baseline_case.rule.report,
        "development_baseline_policy_mismatch",
        mismatches,
        limit,
        retain_baseline=False,
    )


def _rule_mismatches(reference: Rule, cases: Iterable[CalibrationCase]) -> tuple[dict[str, Any], ...]:
    return tuple(mismatch for case in cases for mismatch in [_mismatch(reference, case)] if mismatch is not None)


def _split_rule_cases(
    reference: Rule,
    rule_cases: list[CalibrationCase],
    development_split: str,
    heldout_split: str | None,
) -> tuple[list[CalibrationCase], list[CalibrationCase], list[CalibrationCase]]:
    eligible = [case for case in rule_cases if _mismatch(reference, case) is None]
    development = [case for case in eligible if case.split == development_split]
    heldout = [case for case in eligible if case.split == heldout_split] if heldout_split is not None else []
    return eligible, development, heldout


def _validate_group_split(
    rule_id: str,
    development: list[CalibrationCase],
    heldout: list[CalibrationCase],
    heldout_split: str | None,
) -> None:
    if heldout_split is None:
        return
    missing_heldout = [case.case_id for case in heldout if not _support_key(case)]
    if missing_heldout:
        raise ValueError(
            "heldout cases require explicit support groups for strict split validation: "
            + ", ".join(sorted(missing_heldout))
        )
    development_groups = {_support_key(case) for case in development if _support_key(case)}
    heldout_groups = {_support_key(case) for case in heldout if _support_key(case)}
    overlap = sorted(development_groups & heldout_groups)
    if overlap:
        raise ValueError(f"development and heldout support groups overlap for {rule_id!r}: {', '.join(overlap)}")


def _baseline_rule(
    authoritative: Rule | None,
    eligible: list[CalibrationCase],
    development_split: str,
    baseline: ReportPolicy,
) -> Rule | None:
    if authoritative is not None:
        return authoritative
    if not eligible:
        return None
    references = [case for case in eligible if case.split == development_split] or eligible
    source_rule = min(references, key=lambda case: encode(_semantic_contract(case.rule))).rule
    return Rule.model_validate({
        **source_rule.model_dump(mode="python"),
        "report": _policy_document(baseline),
    })


def _prepare_rule(
    rule_id: str,
    rule_cases: list[CalibrationCase],
    authoritative: Rule | None,
    development_split: str,
    heldout_split: str | None,
    limit: int,
) -> _PreparedRule | RuleSelection:
    development_all = [case for case in rule_cases if case.split == development_split]
    reference = _reference_rule(development_all, authoritative)
    if reference is None:
        return _not_searched_selection(rule_id, None, "no_cases", (), limit, retain_baseline=False)
    baseline = reference.report
    if authoritative is None:
        conflict = _baseline_conflict(rule_id, development_all, limit)
        if conflict is not None:
            return conflict
    mismatches = _rule_mismatches(reference, rule_cases)
    development_mismatches = _rule_mismatches(reference, development_all)
    if authoritative is None and development_mismatches:
        return _not_searched_selection(
            rule_id, baseline, "development_rule_semantics_mismatch", mismatches, limit, retain_baseline=False
        )
    eligible, development, heldout = _split_rule_cases(reference, rule_cases, development_split, heldout_split)
    _validate_group_split(rule_id, development, heldout, heldout_split)
    baseline_rule = _baseline_rule(authoritative, eligible, development_split, baseline)
    if baseline_rule is None:
        return _not_searched_selection(
            rule_id, baseline, "no_compatible_cases", mismatches, limit, retain_baseline=True
        )
    if development and any(not _support_key(case) for case in development):
        return _not_searched_selection(
            rule_id, baseline, "missing_support_groups", mismatches, limit, retain_baseline=True
        )
    return _PreparedRule(rule_id, baseline, baseline_rule, development, heldout, mismatches, development_mismatches)


def _evaluate_prepared_rule(prepared: _PreparedRule, objective: SelectionObjective) -> RuleSelection:
    candidates, generated, truncated = _candidate_policies_with_metadata(
        prepared.baseline_rule,
        prepared.development,
        max_candidates=objective.max_candidates,
    )
    metrics = [
        _candidate_metrics(candidate, prepared.development, objective, rule_id=prepared.rule_id)
        for candidate in candidates
    ]
    baseline_metrics = next(metric for metric in metrics if metric.policy_hash == policy_hash(prepared.baseline))
    sufficient = (
        baseline_metrics.support["positive_groups"] >= objective.min_positive_support
        and baseline_metrics.support["negative_groups"] >= objective.min_negative_support
    )
    chosen, reason = baseline_metrics, "selected"
    if prepared.development_mismatches:
        reason = "question_mismatch"
    elif not prepared.development:
        reason = "no_development_cases"
    elif not sufficient:
        reason = "insufficient_labeled_support"
    else:
        requires_signal = baseline_metrics.label_counts["Agree"] > 0
        if requires_signal and not any(candidate.support["positive_signal"] > 0 for candidate in metrics):
            reason = "no_positive_signal_candidate"
        else:
            chosen = _select_candidate(prepared.baseline, metrics, require_positive_signal=requires_signal)
    chosen_policy = ReportPolicy.model_validate(chosen.policy)
    return RuleSelection(
        prepared.rule_id,
        chosen_policy,
        prepared.baseline,
        reason,
        tuple(metrics),
        baseline_metrics,
        _heldout_metrics(prepared.rule_id, chosen_policy, prepared.heldout),
        prepared.mismatches,
        {
            "limit": objective.max_candidates,
            "generated": generated,
            "truncated": truncated,
            "strategy": "balanced_coordinate_interleave",
        },
        chosen,
    )


def select_policies(
    cases: Iterable[CalibrationCase],
    *,
    development_split: str = "development",
    heldout_split: str | None = None,
    objective: SelectionObjective | Mapping[str, Any] | None = None,
    rules: Mapping[str, Rule] | None = None,
    rule_ids: Iterable[str] | None = None,
) -> SelectionAudit:
    """Select one policy per requested rule using development cases only."""

    if not development_split.strip():
        raise ValueError("development split must be nonempty")
    if heldout_split is not None and not heldout_split.strip():
        raise ValueError("heldout split must be nonempty")
    if heldout_split == development_split:
        raise ValueError("development and heldout splits must differ")
    material = list(cases)
    selection_objective = (
        objective if isinstance(objective, SelectionObjective) else SelectionObjective.from_mapping(objective)
    )
    requested = set(rule_ids or ())
    if rules is not None:
        identifiers = sorted(requested or rules.keys())
    else:
        identifiers = sorted(requested or {case.rule_id for case in material})

    selections: dict[str, RuleSelection] = {}
    for rule_id in identifiers:
        rule_cases = [case for case in material if case.rule_id == rule_id]
        prepared = _prepare_rule(
            rule_id,
            rule_cases,
            rules.get(rule_id) if rules is not None else None,
            development_split,
            heldout_split,
            selection_objective.max_candidates,
        )
        selections[rule_id] = (
            prepared if isinstance(prepared, RuleSelection) else _evaluate_prepared_rule(prepared, selection_objective)
        )
    return SelectionAudit(development_split, heldout_split, selection_objective, selections)
