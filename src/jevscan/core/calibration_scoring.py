"""Score calibration policies against labeled replay cases.

Candidate generation and rule preparation live elsewhere. This module owns the
selection objective, candidate metrics, eligibility, and deterministic winner
choice.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from jevscan.core.calibration_candidates import policy_hash
from jevscan.core.calibration_cases import CalibrationCase
from jevscan.core.calibration_compatibility import support_key
from jevscan.core.rules import ReportPolicy
from jevscan.core.semantic_calibration import ReplayRecord, replay_cases

_LABELS = ("Agree", "Partial", "Disagree")
_OUTCOMES = ("confirmed", "tentative", "none")
_DEFAULT_UTILITY = {
    "Agree": {"confirmed": 4.0, "tentative": 2.0, "none": -4.0},
    "Partial": {"confirmed": -1.0, "tentative": 1.0, "none": 0.0},
    "Disagree": {"confirmed": -8.0, "tentative": -2.0, "none": 0.0},
}


def _policy_document(policy: ReportPolicy) -> dict[str, Any]:
    return policy.model_dump(mode="json")


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
    min_review_list_recall: float | None = None

    @classmethod
    def default(cls) -> SelectionObjective:
        return cls(
            utility={label: dict(values) for label, values in _DEFAULT_UTILITY.items()},
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> SelectionObjective:
        if value is None:
            return cls.default()
        document = _objective_document(value)
        return cls(
            utility=_utility_matrix(document),
            min_positive_support=_positive_int(document.get("min_positive_support", 1), "min_positive_support"),
            min_negative_support=_positive_int(document.get("min_negative_support", 1), "min_negative_support"),
            max_candidates=_positive_int(document.get("max_candidates", 4096), "max_candidates"),
            min_review_list_recall=_optional_fraction(document.get("min_review_list_recall"), "min_review_list_recall"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "utility": {label: dict(self.utility[label]) for label in _LABELS},
            "min_positive_support": self.min_positive_support,
            "min_negative_support": self.min_negative_support,
            "max_candidates": self.max_candidates,
            "min_review_list_recall": self.min_review_list_recall,
        }


_OBJECTIVE_FIELDS = {
    "utility",
    "labels",
    "min_positive_support",
    "min_negative_support",
    "max_candidates",
    "min_review_list_recall",
}


def _objective_document(value: Mapping[str, Any]) -> dict[str, Any]:
    document = dict(value)
    unknown = set(document) - _OBJECTIVE_FIELDS
    if unknown:
        raise ValueError(f"unknown selection objective fields: {', '.join(sorted(unknown))}")
    if "utility" in document and "labels" in document:
        raise ValueError("selection objective must use utility or labels, not both")
    return document


def _utility_value(document: Mapping[str, Any]) -> Mapping[str, Any]:
    value = document.get("utility", document.get("labels"))
    if value is None:
        return {label: dict(values) for label, values in _DEFAULT_UTILITY.items()}
    if not isinstance(value, Mapping):
        raise TypeError("selection objective utility must be a mapping")
    unknown = set(value) - set(_LABELS)
    if unknown:
        raise ValueError(f"unknown selection objective labels: {', '.join(sorted(unknown))}")
    return value


def _numeric_utility(raw: Any, label: str, outcome: str) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise TypeError(f"selection objective {label}.{outcome} must be numeric")
    value = float(raw)
    if not math.isfinite(value):
        raise ValueError(f"selection objective {label}.{outcome} must be finite")
    return value


def _utility_row(utility: Mapping[str, Any], label: str) -> dict[str, float]:
    row = utility.get(label)
    if not isinstance(row, Mapping):
        raise TypeError(f"selection objective is missing {label} utility")
    unknown = set(row) - set(_OUTCOMES)
    if unknown:
        raise ValueError(f"unknown {label} utility outcomes: {', '.join(sorted(unknown))}")
    return {outcome: _numeric_utility(row.get(outcome), label, outcome) for outcome in _OUTCOMES}


def _utility_matrix(document: Mapping[str, Any]) -> dict[str, dict[str, float]]:
    utility = _utility_value(document)
    return {label: _utility_row(utility, label) for label in _LABELS}


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"selection objective {name} must be a positive integer")
    return value


def _optional_fraction(value: Any, name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"selection objective {name} must be numeric")
    result = float(value)
    if not math.isfinite(result) or not 0.0 <= result <= 1.0:
        raise ValueError(f"selection objective {name} must be finite and between 0 and 1")
    return result


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


def _review_list_recall(records: Iterable[ReplayRecord]) -> float | None:
    """Return group-normalized Agree review-list recall.

    A support group is retained when at least one of its Agree cases produces
    a confirmed or tentative finding. Selection already requires explicit
    support groups, so a missing group is not silently treated as a group of
    its own here.
    """
    positive_groups: set[str] = set()
    retained_groups: set[str] = set()
    for record in records:
        if record.case.label != "Agree":
            continue
        group = support_key(record.case)
        if not group:
            continue
        positive_groups.add(group)
        if record.signal:
            retained_groups.add(group)
    if not positive_groups:
        return None
    return len(retained_groups) / len(positive_groups)


def _outcome(record: ReplayRecord) -> str:
    if record.confirmed:
        return "confirmed"
    if record.assessment.tentative_finding is not None:
        return "tentative"
    return "none"


def _label_counts(records: list[ReplayRecord]) -> dict[str, int]:
    return {label: sum(record.case.label == label for record in records) for label in _LABELS}


def _outcome_counts(records: list[ReplayRecord]) -> dict[str, dict[str, int]]:
    return {
        label: {
            outcome: sum(record.case.label == label and _outcome(record) == outcome for record in records)
            for outcome in _OUTCOMES
        }
        for label in _LABELS
    }


def _group_sizes(records: list[ReplayRecord]) -> dict[str, int]:
    sizes: dict[str, int] = defaultdict(int)
    for record in records:
        group = support_key(record.case)
        if group:
            sizes[group] += 1
    return sizes


def _weighted_utility(
    records: list[ReplayRecord],
    objective: SelectionObjective,
    group_sizes: Mapping[str, int],
) -> float:
    return sum(
        objective.utility[record.case.label][_outcome(record)] / group_sizes[support_key(record.case)]
        for record in records
        if record.case.label in objective.utility and support_key(record.case)
    )


def _support_groups(records: list[ReplayRecord]) -> tuple[dict[str, int], int]:
    groups_by_label: dict[str, set[str]] = defaultdict(set)
    missing = 0
    for record in records:
        group = support_key(record.case)
        if group:
            groups_by_label[record.case.label].add(group)
        else:
            missing += 1
    return {label: len(groups_by_label[label]) for label in _LABELS}, missing


def _support_summary(
    records: list[ReplayRecord],
    total_records: int,
    outcome_counts: dict[str, dict[str, int]],
) -> dict[str, Any]:
    group_counts, missing = _support_groups(records)
    return {
        "positive_groups": group_counts["Agree"],
        "negative_groups": group_counts["Disagree"],
        "partial_groups": group_counts["Partial"],
        "groups_by_label": group_counts,
        "missing_support_groups": missing,
        "positive_signal": outcome_counts["Agree"]["confirmed"] + outcome_counts["Agree"]["tentative"],
        "review_list_recall": _review_list_recall(records),
        "non_comparable": total_records - len(records),
    }


def candidate_metrics(
    policy: ReportPolicy,
    cases: list[CalibrationCase],
    objective: SelectionObjective,
    *,
    rule_id: str,
) -> CandidateMetrics:
    report = replay_cases(cases, {rule_id: policy})
    usable = [record for record in report.records if record.comparable]
    outcomes = _outcome_counts(usable)
    return CandidateMetrics(
        policy=_policy_document(policy),
        policy_hash=policy_hash(policy),
        objective=_weighted_utility(usable, objective, _group_sizes(usable)),
        records=len(report.records),
        comparable_records=len(usable),
        label_counts=_label_counts(usable),
        outcome_counts=outcomes,
        support=_support_summary(usable, len(report.records), outcomes),
    )

def _policy_distance(left: Any, right: Any) -> int:
    def distance(first: Any, second: Any) -> int:
        if isinstance(first, Mapping) and isinstance(second, Mapping):
            return sum(distance(first.get(key), second.get(key)) for key in set(first) | set(second))
        if isinstance(first, list) and isinstance(second, list):
            shared = min(len(first), len(second))
            return sum(distance(first[index], second[index]) for index in range(shared)) + abs(len(first) - len(second))
        return int(first != second)

    return distance(_policy_document(left), _policy_document(right))


def candidate_is_eligible(
    candidate: CandidateMetrics,
    *,
    require_positive_signal: bool,
    objective: SelectionObjective,
) -> bool:
    if require_positive_signal and candidate.support["positive_signal"] <= 0:
        return False
    minimum_recall = objective.min_review_list_recall
    return minimum_recall is None or (
        candidate.support["review_list_recall"] is not None
        and candidate.support["review_list_recall"] >= minimum_recall
    )


def select_candidate(
    baseline: ReportPolicy,
    candidates: list[CandidateMetrics],
    *,
    require_positive_signal: bool,
    objective: SelectionObjective,
) -> CandidateMetrics:
    baseline_hash = policy_hash(baseline)
    eligible = [
        candidate
        for candidate in candidates
        if candidate_is_eligible(candidate, require_positive_signal=require_positive_signal, objective=objective)
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
