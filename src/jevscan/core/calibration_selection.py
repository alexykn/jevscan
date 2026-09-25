"""Orchestrate deterministic reporting-policy selection.

Input contracts, compatibility checks, candidate generation, scoring, replay,
and per-rule preparation each have dedicated owners. This module coordinates
those stages and preserves the public calibration-selection API.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from jevscan.core.calibration_candidates import candidate_policies, candidate_policies_with_metadata, policy_hash
from jevscan.core.calibration_cases import CalibrationCase
from jevscan.core.calibration_compatibility import (
    CompatibilityCheck,
    SelectionCompatibility,
    compatibility_check,
    compatibility_check_against,
    compatibility_reason,
)
from jevscan.core.calibration_preparation import (
    PreparedRule,
    fit_development_cases,
    heldout_metrics as evaluate_heldout_metrics,
    not_searched_selection,
    prepare_rule,
)
from jevscan.core.calibration_scoring import (
    CandidateMetrics,
    SelectionObjective,
    candidate_is_eligible,
    candidate_metrics,
    select_candidate,
)
from jevscan.core.calibration_selection_models import (
    RuleSelection,
    SelectionAudit,
    selected_policy_document,
)
from jevscan.core.rules import ReportPolicy, Rule


def _choose_candidate(
    prepared: PreparedRule,
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
        if candidate_is_eligible(candidate, require_positive_signal=requires_signal, objective=objective)
    ]
    if requires_signal and not any(candidate.support["positive_signal"] > 0 for candidate in metrics):
        return baseline_metrics, "no_positive_signal_candidate"
    if objective.min_review_list_recall is not None and not eligible:
        return baseline_metrics, "no_candidate_meets_review_list_recall"
    return (
        select_candidate(
            prepared.baseline,
            metrics,
            require_positive_signal=requires_signal,
            objective=objective,
        ),
        "selected",
    )


def _evaluate_prepared_rule(prepared: PreparedRule, objective: SelectionObjective) -> RuleSelection:
    candidates, generated, truncated = candidate_policies_with_metadata(
        prepared.baseline_rule,
        prepared.development,
        max_candidates=objective.max_candidates,
    )
    metrics = [
        candidate_metrics(candidate, prepared.development, objective, rule_id=prepared.rule_id)
        for candidate in candidates
    ]
    baseline_hash = policy_hash(prepared.baseline)
    baseline_metrics = next(metric for metric in metrics if metric.policy_hash == baseline_hash)
    chosen, reason = _choose_candidate(prepared, metrics, baseline_metrics, objective)
    chosen_policy = ReportPolicy.model_validate(chosen.policy)
    heldout_compatibility = compatibility_check_against(prepared.compatibility.authority, prepared.heldout)
    heldout_result = evaluate_heldout_metrics(
        prepared.rule_id,
        chosen_policy,
        prepared.heldout,
        heldout_compatibility,
    )

    return RuleSelection(
        prepared.rule_id,
        chosen_policy,
        prepared.baseline,
        reason,
        tuple(metrics),
        baseline_metrics,
        heldout_result,
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


def _select_prepared_rule(
    rule_id: str,
    prepared: PreparedRule | RuleSelection,
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
    return not_searched_selection(
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
        fit_development_cases(cases_by_rule, identifiers, rules, development_split)
    )
    selections: dict[str, RuleSelection] = {}
    for rule_id in identifiers:
        prepared = prepare_rule(
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

