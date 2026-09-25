"""Prepare one rule's calibration material before candidate scoring."""

import hashlib
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from jevscan.core.calibration_cases import CalibrationCase
from jevscan.core.calibration_compatibility import (
    CompatibilityCheck,
    compatibility_check,
    compatibility_reason,
    support_key,
)
from jevscan.core.calibration_selection_models import RuleSelection
from jevscan.core.protocol import encode
from jevscan.core.rules import ReportPolicy, Rule
from jevscan.core.semantic_calibration import CalibrationReport, replay_cases


@dataclass(frozen=True, slots=True)
class PreparationMaterial:
    eligible: list[CalibrationCase]
    development: list[CalibrationCase]
    heldout: list[CalibrationCase]
    mismatches: tuple[dict[str, Any], ...]
    development_mismatches: tuple[dict[str, Any], ...]
    compatibility: CompatibilityCheck


@dataclass(frozen=True, slots=True)
class PreparedRule:
    rule_id: str
    baseline: ReportPolicy
    baseline_rule: Rule
    development: list[CalibrationCase]
    heldout: list[CalibrationCase]
    mismatches: tuple[dict[str, Any], ...]
    development_mismatches: tuple[dict[str, Any], ...]
    compatibility: CompatibilityCheck


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


def heldout_metrics(
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


def not_searched_selection(
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
    return not_searched_selection(
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
        return not_searched_selection(rule_id, None, "no_cases", (), limit, retain_baseline=False)
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
) -> PreparationMaterial | RuleSelection:
    baseline = reference.report
    mismatches = _rule_mismatches(reference, rule_cases)
    development_mismatches = _rule_mismatches(reference, development_all)
    if authoritative is None and development_mismatches:
        return not_searched_selection(
            rule_id, baseline, "development_rule_semantics_mismatch", mismatches, limit, retain_baseline=False
        )

    eligible, development, heldout = _split_rule_cases(reference, rule_cases, development_split, heldout_split)
    compatibility = compatibility_check(development)
    rejected = compatibility.incompatible and (
        compatibility.has_missing_metadata or not allow_incompatible_model_prompt
    )
    if rejected:
        return not_searched_selection(
            rule_id,
            baseline,
            compatibility_reason(compatibility, "development"),
            mismatches,
            limit,
            retain_baseline=False,
            compatibility=compatibility,
        )
    _validate_group_split(rule_id, development, heldout, heldout_split)
    return PreparationMaterial(
        eligible,
        development,
        heldout,
        mismatches,
        development_mismatches,
        compatibility,
    )


def prepare_rule(
    rule_id: str,
    rule_cases: list[CalibrationCase],
    authoritative: Rule | None,
    development_split: str,
    heldout_split: str | None,
    limit: int,
    allow_incompatible_model_prompt: bool,
) -> PreparedRule | RuleSelection:
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
        return not_searched_selection(
            rule_id, baseline, "no_compatible_cases", material.mismatches, limit, retain_baseline=True
        )
    if material.development and any(not support_key(case) for case in material.development):
        return not_searched_selection(
            rule_id, baseline, "missing_support_groups", material.mismatches, limit, retain_baseline=True
        )
    return PreparedRule(
        rule_id,
        baseline,
        baseline_rule,
        material.development,
        material.heldout,
        material.mismatches,
        material.development_mismatches,
        material.compatibility,
    )


def fit_development_cases(
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
