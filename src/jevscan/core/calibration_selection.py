"""Deterministic offline selection of reporting thresholds.

This module deliberately does not interpret answers itself.  Candidate policies
are evaluated by :func:`jevscan.core.semantic_calibration.replay_cases`, which
reuses the production ``Check`` and ``assess`` path.  The selector only
generates valid reporting-policy variants and scores their observed outcomes.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from jevscan.core.protocol import encode
from jevscan.core.rules import ReportPolicy, Rule
from jevscan.core.calibration_candidates import _candidate_policies_with_metadata, candidate_policies, policy_hash
from jevscan.core.calibration_scoring import (
    CandidateMetrics,
    SelectionObjective,
    _candidate_is_eligible,
    _candidate_metrics,
    _select_candidate,
)
from jevscan.core.calibration_compatibility import (
    CompatibilityCheck,
    SelectionCompatibility,
    compatibility_check,
    compatibility_check_against,
    compatibility_reason,
    support_key,
)
from jevscan.core.semantic_calibration import CalibrationCase, CalibrationReport, ReplayRecord, replay_cases

SELECTION_VERSION = 1
def _policy_document(policy: ReportPolicy) -> dict[str, Any]:
    return policy.model_dump(mode="json")


@dataclass(frozen=True, slots=True)
class _PreparationMaterial:
    eligible: list[CalibrationCase]
    development: list[CalibrationCase]
    heldout: list[CalibrationCase]
    mismatches: tuple[dict[str, Any], ...]
    development_mismatches: tuple[dict[str, Any], ...]
    compatibility: CompatibilityCheck


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
    missing_heldout = [case.case_id for case in heldout if not support_key(case)]
    if missing_heldout:
        raise ValueError(
            "heldout cases require explicit support groups for strict split validation: "
            + ", ".join(sorted(missing_heldout))
        )
    development_groups = {support_key(case) for case in development if support_key(case)}
    heldout_groups = {support_key(case) for case in heldout if support_key(case)}
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
) -> _PreparationMaterial | RuleSelection:
    baseline = reference.report
    mismatches = _rule_mismatches(reference, rule_cases)
    development_mismatches = _rule_mismatches(reference, development_all)
    if authoritative is None and development_mismatches:
        return _not_searched_selection(
            rule_id, baseline, "development_rule_semantics_mismatch", mismatches, limit, retain_baseline=False
        )

    eligible, development, heldout = _split_rule_cases(reference, rule_cases, development_split, heldout_split)
    compatibility = compatibility_check(development)
    rejected = compatibility.incompatible and (
        compatibility.has_missing_metadata or not allow_incompatible_model_prompt
    )
    if rejected:
        return _not_searched_selection(
            rule_id,
            baseline,
            compatibility_reason(compatibility, "development"),
            mismatches,
            limit,
            retain_baseline=False,
            compatibility=compatibility,
        )
    _validate_group_split(rule_id, development, heldout, heldout_split)
    return _PreparationMaterial(
        eligible,
        material.development,
        material.heldout,
        material.mismatches,
        material.development_mismatches,
        material.compatibility,
    )


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
    baseline = reference.report
    baseline_rule = _baseline_rule(authoritative, material.eligible, development_split, baseline)
    if baseline_rule is None:
        return _not_searched_selection(
            rule_id, baseline, "no_compatible_cases", material.mismatches, limit, retain_baseline=True
        )
    if material.development and any(not support_key(case) for case in material.development):
        return _not_searched_selection(
            rule_id, baseline, "missing_support_groups", material.mismatches, limit, retain_baseline=True
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
    heldout_compatibility = compatibility_check_against(prepared.compatibility.authority, prepared.heldout)
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
        compatibility_reason(fit_compatibility, "requested_fit"),
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
    fit_compatibility = compatibility_check(
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
