"""Deterministic offline selection of reporting thresholds.

This module deliberately does not interpret answers itself.  Candidate policies
are evaluated by :func:`jevscan.core.semantic_calibration.replay_cases`, which
reuses the production ``Check`` and ``assess`` path.  The selector only
generates valid reporting-policy variants and scores their observed outcomes.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from jevscan.core.protocol import encode
from jevscan.core.rules import ReportPolicy, Rule
from jevscan.core.calibration_candidates import _candidate_policies_with_metadata, candidate_policies, policy_hash
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
        document = dict(value)
        unknown = set(document) - {
            "utility",
            "labels",
            "min_positive_support",
            "min_negative_support",
            "max_candidates",
            "min_review_list_recall",
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
        group = _support_key(record.case)
        if not group:
            continue
        positive_groups.add(group)
        if record.signal:
            retained_groups.add(group)
    if not positive_groups:
        return None
    return len(retained_groups) / len(positive_groups)


@dataclass(frozen=True, slots=True)
class _CompatibilityAuthority:
    """Frozen metadata authority for one selection scope."""

    returned_model: str
    prompt_version: int
    prompt_policy: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "model_identity": "returned_model",
            "returned_model": self.returned_model,
            "prompt": {
                "version": self.prompt_version,
                "policy": self.prompt_policy,
            },
        }


@dataclass(frozen=True, slots=True)
class CompatibilityCheck:
    """Selection-only compatibility evidence for one set of cases."""

    status: str
    authority: _CompatibilityAuthority | None
    checked_case_ids: tuple[str, ...]
    mismatches: tuple[dict[str, Any], ...] = ()

    @property
    def incompatible_case_ids(self) -> tuple[str, ...]:
        return tuple(sorted({str(item["case_id"]) for item in self.mismatches}))

    @property
    def has_missing_metadata(self) -> bool:
        return any(item.get("reason") == "missing_metadata" for item in self.mismatches)

    @property
    def incompatible(self) -> bool:
        return bool(self.mismatches)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "authority": self.authority.as_dict() if self.authority is not None else None,
            "checked_case_ids": list(self.checked_case_ids),
            "rejected_case_ids": list(self.incompatible_case_ids),
            "mismatches": list(self.mismatches),
        }


@dataclass(frozen=True, slots=True)
class SelectionCompatibility:
    """Compatibility policy recorded for a complete requested fit."""

    allow_incompatible_model_prompt: bool
    development: CompatibilityCheck

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": (
                "allow_incompatible_model_prompt" if self.allow_incompatible_model_prompt else "strict_model_prompt"
            ),
            "model_identity": "returned_model",
            "prompt_identity": ["version", "policy"],
            "requested_model": "not_used_for_compatibility",
            "allow_incompatible_model_prompt": self.allow_incompatible_model_prompt,
            "development": self.development.as_dict(),
        }


def _compatibility_reason(check: CompatibilityCheck, scope: str) -> str:
    if check.has_missing_metadata:
        return f"{scope}_compatibility_metadata_missing"
    fields = {str(item["field"]) for item in check.mismatches}
    model = "returned_model" in fields
    prompt = bool(fields & {"prompt.version", "prompt.policy"})
    if model and not prompt:
        return f"{scope}_model_mismatch"
    if prompt and not model:
        return f"{scope}_prompt_mismatch"
    return f"{scope}_model_prompt_mismatch"


@dataclass(frozen=True, slots=True)
class _PreparedRule:
    rule_id: str
    baseline: ReportPolicy
    baseline_rule: Rule
    development: list[CalibrationCase]
    heldout: list[CalibrationCase]
    mismatches: tuple[dict[str, Any], ...]
    development_mismatches: tuple[dict[str, Any], ...]
    compatibility: CompatibilityCheck


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
    compatibility: CompatibilityCheck | None = None

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
            "compatibility": self.compatibility.as_dict() if self.compatibility is not None else None,
        }


@dataclass(frozen=True, slots=True)
class SelectionAudit:
    development_split: str
    heldout_split: str | None
    objective: SelectionObjective
    rules: dict[str, RuleSelection]
    compatibility: SelectionCompatibility

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
            "compatibility": self.compatibility.as_dict(),
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


def _compatibility_values(case: CalibrationCase) -> tuple[str | None, int | None, str | None]:
    returned_model = (
        case.returned_model if isinstance(case.returned_model, str) and case.returned_model.strip() else None
    )
    prompt_version = case.prompt.version if isinstance(case.prompt.version, int) and case.prompt.version >= 1 else None
    prompt_policy = case.prompt.policy if isinstance(case.prompt.policy, str) and case.prompt.policy.strip() else None
    return returned_model, prompt_version, prompt_policy


def _missing_compatibility_mismatches(case: CalibrationCase) -> list[dict[str, Any]]:
    returned_model, prompt_version, prompt_policy = _compatibility_values(case)
    missing: list[dict[str, Any]] = []
    for field, actual in (
        ("returned_model", returned_model),
        ("prompt.version", prompt_version),
        ("prompt.policy", prompt_policy),
    ):
        if actual is None:
            missing.append({
                "case_id": case.case_id,
                "field": field,
                "reason": "missing_metadata",
                "actual": None,
            })
    return missing


def _partition_compatibility_cases(
    cases: Iterable[CalibrationCase],
) -> tuple[list[CalibrationCase], tuple[dict[str, Any], ...]]:
    complete: list[CalibrationCase] = []
    missing: list[dict[str, Any]] = []
    for case in cases:
        case_missing = _missing_compatibility_mismatches(case)
        if case_missing:
            missing.extend(case_missing)
        else:
            complete.append(case)
    return complete, tuple(missing)


def _compatibility_mismatches(
    authority: _CompatibilityAuthority,
    case: CalibrationCase,
) -> list[dict[str, Any]]:
    returned_model, prompt_version, prompt_policy = _compatibility_values(case)
    actual_values = {
        "returned_model": returned_model,
        "prompt.version": prompt_version,
        "prompt.policy": prompt_policy,
    }
    expected_values = {
        "returned_model": authority.returned_model,
        "prompt.version": authority.prompt_version,
        "prompt.policy": authority.prompt_policy,
    }
    return [
        {
            "case_id": case.case_id,
            "field": field,
            "expected": expected,
            "actual": actual_values[field],
        }
        for field, expected in expected_values.items()
        if actual_values[field] is None or actual_values[field] != expected
    ]


def _compatibility_check(cases: Iterable[CalibrationCase]) -> CompatibilityCheck:
    material = sorted(cases, key=lambda case: (case.case_id, case.split))
    checked_case_ids = tuple(case.case_id for case in material)
    if not material:
        return CompatibilityCheck("no_cases", None, checked_case_ids)
    complete, missing = _partition_compatibility_cases(material)
    if not complete:
        return CompatibilityCheck("missing_metadata", None, checked_case_ids, missing)
    first = complete[0]
    returned_model, prompt_version, prompt_policy = _compatibility_values(first)
    assert returned_model is not None and prompt_version is not None and prompt_policy is not None
    authority = _CompatibilityAuthority(returned_model, prompt_version, prompt_policy)
    mismatches = missing + tuple(
        mismatch for case in complete for mismatch in _compatibility_mismatches(authority, case)
    )
    status = "missing_metadata" if missing else "compatible" if not mismatches else "mismatch"
    return CompatibilityCheck(status, authority, checked_case_ids, mismatches)


def _compatibility_check_against(
    authority: _CompatibilityAuthority | None,
    cases: Iterable[CalibrationCase],
) -> CompatibilityCheck:
    material = sorted(cases, key=lambda case: (case.case_id, case.split))
    checked_case_ids = tuple(case.case_id for case in material)
    if authority is None:
        return CompatibilityCheck("no_development_authority", None, checked_case_ids)
    complete, missing = _partition_compatibility_cases(material)
    mismatches = missing + tuple(
        mismatch for case in complete for mismatch in _compatibility_mismatches(authority, case)
    )
    status = "missing_metadata" if missing else "compatible" if not mismatches else "mismatch"
    return CompatibilityCheck(status, authority, checked_case_ids, mismatches)


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
    review_list_recall = _review_list_recall(usable)
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
            "review_list_recall": review_list_recall,
            "non_comparable": len(report.records) - len(usable),
        },
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


def _candidate_is_eligible(
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


def _select_candidate(
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
        if _candidate_is_eligible(candidate, require_positive_signal=require_positive_signal, objective=objective)
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
    compatibility: CompatibilityCheck,
) -> dict[str, Any] | None:
    if not cases:
        return None
    excluded = set(compatibility.incompatible_case_ids)
    report_cases = [case for case in cases if case.case_id not in excluded]
    report: CalibrationReport = replay_cases(report_cases, {rule_id: policy})
    return {
        "split": cases[0].split,
        "rules": {split: split_report.as_dict() for split, split_report in report.reports.get(rule_id, {}).items()},
        "non_comparable": list(report.non_comparable),
        "compatibility": compatibility.as_dict(),
    }


def _not_searched_selection(
    rule_id: str,
    baseline: ReportPolicy | None,
    reason: str,
    mismatches: tuple[dict[str, Any], ...],
    limit: int,
    *,
    retain_baseline: bool,
    compatibility: CompatibilityCheck | None = None,
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
        None,
        compatibility,
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


def _preparation_reference(
    rule_id: str,
    rule_cases: list[CalibrationCase],
    development_split: str,
    authoritative: Rule | None,
    limit: int,
) -> tuple[Rule, list[CalibrationCase]] | RuleSelection:
    development = [case for case in rule_cases if case.split == development_split]
    reference = _reference_rule(development, authoritative)
    if reference is None:
        return _not_searched_selection(rule_id, None, "no_cases", (), limit, retain_baseline=False)
    if authoritative is None:
        conflict = _baseline_conflict(rule_id, development, limit)
        if conflict is not None:
            return conflict
    return reference, development


def _preparation_material(
    rule_id: str,
    rule_cases: list[CalibrationCase],
    reference: Rule,
    development_all: list[CalibrationCase],
    authoritative: Rule | None,
    development_split: str,
    heldout_split: str | None,
    limit: int,
    allow_incompatible_model_prompt: bool,
) -> tuple[list[CalibrationCase], list[CalibrationCase], list[CalibrationCase], tuple[dict[str, Any], ...], tuple[dict[str, Any], ...], CompatibilityCheck] | RuleSelection:
    baseline = reference.report
    mismatches = _rule_mismatches(reference, rule_cases)
    development_mismatches = _rule_mismatches(reference, development_all)
    if authoritative is None and development_mismatches:
        return _not_searched_selection(
            rule_id, baseline, "development_rule_semantics_mismatch", mismatches, limit, retain_baseline=False
        )

    eligible, development, heldout = _split_rule_cases(reference, rule_cases, development_split, heldout_split)
    compatibility = _compatibility_check(development)
    rejected = compatibility.incompatible and (
        compatibility.has_missing_metadata or not allow_incompatible_model_prompt
    )
    if rejected:
        return _not_searched_selection(
            rule_id,
            baseline,
            _compatibility_reason(compatibility, "development"),
            mismatches,
            limit,
            retain_baseline=False,
            compatibility=compatibility,
        )
    _validate_group_split(rule_id, development, heldout, heldout_split)
    return eligible, development, heldout, mismatches, development_mismatches, compatibility


def _prepare_rule(
    rule_id: str,
    rule_cases: list[CalibrationCase],
    authoritative: Rule | None,
    development_split: str,
    heldout_split: str | None,
    limit: int,
    allow_incompatible_model_prompt: bool,
) -> _PreparedRule | RuleSelection:
    reference_result = _preparation_reference(rule_id, rule_cases, development_split, authoritative, limit)
    if isinstance(reference_result, RuleSelection):
        return reference_result
    reference, development_all = reference_result

    material = _preparation_material(
        rule_id,
        rule_cases,
        reference,
        development_all,
        authoritative,
        development_split,
        heldout_split,
        limit,
        allow_incompatible_model_prompt,
    )
    if isinstance(material, RuleSelection):
        return material
    eligible, development, heldout, mismatches, development_mismatches, compatibility = material

    baseline = reference.report
    baseline_rule = _baseline_rule(authoritative, eligible, development_split, baseline)
    if baseline_rule is None:
        return _not_searched_selection(
            rule_id, baseline, "no_compatible_cases", mismatches, limit, retain_baseline=True
        )
    if development and any(not _support_key(case) for case in development):
        return _not_searched_selection(
            rule_id, baseline, "missing_support_groups", mismatches, limit, retain_baseline=True
        )
    return _PreparedRule(
        rule_id,
        baseline,
        baseline_rule,
        development,
        heldout,
        mismatches,
        development_mismatches,
        compatibility,
    )


def _choose_candidate(
    prepared: _PreparedRule,
    metrics: list[CandidateMetrics],
    baseline_metrics: CandidateMetrics,
    objective: SelectionObjective,
) -> tuple[CandidateMetrics, str]:
    if prepared.development_mismatches:
        return baseline_metrics, "question_mismatch"
    if not prepared.development:
        return baseline_metrics, "no_development_cases"

    sufficient = (
        baseline_metrics.support["positive_groups"] >= objective.min_positive_support
        and baseline_metrics.support["negative_groups"] >= objective.min_negative_support
    )
    if not sufficient:
        return baseline_metrics, "insufficient_labeled_support"

    requires_signal = baseline_metrics.label_counts["Agree"] > 0
    eligible = [
        candidate
        for candidate in metrics
        if _candidate_is_eligible(candidate, require_positive_signal=requires_signal, objective=objective)
    ]
    if requires_signal and not any(candidate.support["positive_signal"] > 0 for candidate in metrics):
        return baseline_metrics, "no_positive_signal_candidate"
    if objective.min_review_list_recall is not None and not eligible:
        return baseline_metrics, "no_candidate_meets_review_list_recall"
    return (
        _select_candidate(
            prepared.baseline,
            metrics,
            require_positive_signal=requires_signal,
            objective=objective,
        ),
        "selected",
    )


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
    baseline_hash = policy_hash(prepared.baseline)
    baseline_metrics = next(metric for metric in metrics if metric.policy_hash == baseline_hash)
    chosen, reason = _choose_candidate(prepared, metrics, baseline_metrics, objective)
    chosen_policy = ReportPolicy.model_validate(chosen.policy)
    heldout_compatibility = _compatibility_check_against(prepared.compatibility.authority, prepared.heldout)
    heldout_metrics = _heldout_metrics(prepared.rule_id, chosen_policy, prepared.heldout, heldout_compatibility)

    return RuleSelection(
        prepared.rule_id,
        chosen_policy,
        prepared.baseline,
        reason,
        tuple(metrics),
        baseline_metrics,
        heldout_metrics,
        prepared.mismatches,
        {
            "limit": objective.max_candidates,
            "generated": generated,
            "truncated": truncated,
            "strategy": "balanced_coordinate_interleave",
        },
        chosen,
        prepared.compatibility,
    )

def _validate_selection_splits(development_split: str, heldout_split: str | None) -> None:
    if not development_split.strip():
        raise ValueError("development split must be nonempty")
    if heldout_split is not None and not heldout_split.strip():
        raise ValueError("heldout split must be nonempty")
    if heldout_split == development_split:
        raise ValueError("development and heldout splits must differ")


def _requested_rule_ids(
    material: list[CalibrationCase],
    rules: Mapping[str, Rule] | None,
    rule_ids: Iterable[str] | None,
) -> list[str]:
    requested = set(rule_ids or ())
    available = rules.keys() if rules is not None else (case.rule_id for case in material)
    return sorted(requested or set(available))


def _group_cases_by_rule(
    material: list[CalibrationCase],
    identifiers: Iterable[str],
) -> dict[str, list[CalibrationCase]]:
    requested = set(identifiers)
    result: dict[str, list[CalibrationCase]] = {rule_id: [] for rule_id in requested}
    for case in material:
        if case.rule_id in requested:
            result[case.rule_id].append(case)
    return result


def _fit_development_cases(
    cases_by_rule: Mapping[str, list[CalibrationCase]],
    identifiers: Iterable[str],
    rules: Mapping[str, Rule] | None,
    development_split: str,
) -> list[CalibrationCase]:
    result: list[CalibrationCase] = []
    for rule_id in identifiers:
        development = [case for case in cases_by_rule[rule_id] if case.split == development_split]
        authoritative = rules.get(rule_id) if rules is not None else None
        reference = _reference_rule(development, authoritative)
        if reference is not None:
            result.extend(case for case in development if _mismatch(reference, case) is None)
    return result


def _select_prepared_rule(
    rule_id: str,
    prepared: _PreparedRule | RuleSelection,
    fit_compatibility: CompatibilityCheck,
    objective: SelectionObjective,
    allow_incompatible_model_prompt: bool,
) -> RuleSelection:
    if isinstance(prepared, RuleSelection):
        return prepared
    fit_rejected = fit_compatibility.incompatible and (
        fit_compatibility.has_missing_metadata or not allow_incompatible_model_prompt
    )
    if not fit_rejected:
        return _evaluate_prepared_rule(prepared, objective)
    return _not_searched_selection(
        rule_id,
        prepared.baseline,
        _compatibility_reason(fit_compatibility, "requested_fit"),
        prepared.mismatches,
        objective.max_candidates,
        retain_baseline=False,
        compatibility=fit_compatibility,
    )


def select_policies(
    cases: Iterable[CalibrationCase],
    *,
    development_split: str = "development",
    heldout_split: str | None = None,
    objective: SelectionObjective | Mapping[str, Any] | None = None,
    rules: Mapping[str, Rule] | None = None,
    rule_ids: Iterable[str] | None = None,
    allow_incompatible_model_prompt: bool = False,
) -> SelectionAudit:
    """Select one policy per requested rule using development cases only.

    Returned concrete model identity and prompt version/policy are strict
    compatibility authorities by default. The opt-in exists for audited
    historical experiments; it never substitutes requested model aliases and
    never permits missing metadata.
    """

    _validate_selection_splits(development_split, heldout_split)
    material = list(cases)
    selection_objective = (
        objective if isinstance(objective, SelectionObjective) else SelectionObjective.from_mapping(objective)
    )
    identifiers = _requested_rule_ids(material, rules, rule_ids)
    cases_by_rule = _group_cases_by_rule(material, identifiers)
    fit_compatibility = _compatibility_check(
        _fit_development_cases(cases_by_rule, identifiers, rules, development_split)
    )
    selections: dict[str, RuleSelection] = {}
    for rule_id in identifiers:
        prepared = _prepare_rule(
            rule_id,
            cases_by_rule[rule_id],
            rules.get(rule_id) if rules is not None else None,
            development_split,
            heldout_split,
            selection_objective.max_candidates,
            allow_incompatible_model_prompt,
        )
        selections[rule_id] = _select_prepared_rule(
            rule_id,
            prepared,
            fit_compatibility,
            selection_objective,
            allow_incompatible_model_prompt,
        )
    return SelectionAudit(
        development_split,
        heldout_split,
        selection_objective,
        selections,
        SelectionCompatibility(allow_incompatible_model_prompt, fit_compatibility),
    )
